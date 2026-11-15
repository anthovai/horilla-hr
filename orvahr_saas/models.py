"""
Tenant registry, stored in the public schema.

A Tenant row *is* a PostgreSQL schema: saving a new Tenant creates the schema
and runs every tenant migration in it (django-tenants ``auto_create_schema``).
Use ``manage.py create_tenant`` rather than creating rows by hand -- it also
creates the first company and administrator inside the new schema.
"""

import re

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import get_language
from django.utils.translation import gettext_lazy as _
from django_tenants.models import DomainMixin, TenantMixin

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")

# Schema names a tenant may never take: PostgreSQL's own, the shared schema,
# and names that would read as a system schema in a dump or an alert.
RESERVED_SCHEMAS = {
    "public",
    "information_schema",
    "pg_catalog",
    "pg_toast",
    "admin",
    "www",
    "api",
    "static",
    "media",
}

# Sub-domains a customer may never ask for on the sign-up form, on top of the
# reserved schemas: the control plane's own hosts and names that would look
# official in a phishing mail ("support.orvahr.example").
RESERVED_SLUGS = RESERVED_SCHEMAS | {
    "app",
    "billing",
    "blog",
    "console",
    "dashboard",
    "demo",
    "docs",
    "help",
    "login",
    "mail",
    "manage",
    "ns1",
    "ns2",
    "orvahr",
    "root",
    "signup",
    "smtp",
    "status",
    "support",
    "system",
    "test",
}


def schema_name_for_slug(slug: str) -> str:
    """``acme-th`` -> ``acme_th`` (hyphens are not plain SQL identifiers)."""
    return slug.replace("-", "_")


def validate_slug(slug: str) -> None:
    if not SLUG_RE.match(slug or ""):
        raise ValidationError(
            _(
                "Slug must be 2-41 characters: lowercase letters, digits and "
                "hyphens, starting with a letter."
            )
        )
    if slug in RESERVED_SCHEMAS or schema_name_for_slug(slug) in RESERVED_SCHEMAS:
        raise ValidationError(_("%(slug)s is reserved.") % {"slug": slug})
    if schema_name_for_slug(slug).startswith("pg_"):
        raise ValidationError(_("Slugs may not start with 'pg-'."))


def _default_modules():
    return []


class Plan(models.Model):
    """
    What a customer may use: an employee ceiling and a set of optional
    modules (app labels, see ``orvahr_saas.plans.MODULE_CATALOG``). Prices are
    NOT here -- they belong to orvahr_billing, which reads plans by ``code``.
    Shared (public schema); a tenant without a plan is unrestricted.
    """

    code = models.SlugField(_("Code"), max_length=40, unique=True)
    name_en = models.CharField(_("Name (English)"), max_length=100)
    name_th = models.CharField(_("Name (Thai)"), max_length=100, blank=True)
    description_en = models.TextField(_("Description (English)"), blank=True)
    description_th = models.TextField(_("Description (Thai)"), blank=True)
    max_employees = models.PositiveIntegerField(
        _("Maximum active employees"),
        null=True,
        blank=True,
        help_text=_("Empty means unlimited."),
    )
    modules = models.JSONField(
        _("Enabled modules"),
        default=_default_modules,
        blank=True,
        help_text=_("App labels of the optional modules this plan includes."),
    )
    trial_days = models.PositiveIntegerField(_("Trial days"), default=14)
    is_public = models.BooleanField(_("Shown on the public site"), default=True)
    sort = models.PositiveIntegerField(_("Sort order"), default=0)

    class Meta:
        ordering = ["sort", "code"]

    def __str__(self):
        return self.display_name

    @property
    def display_name(self):
        if (get_language() or "").startswith("th") and self.name_th:
            return self.name_th
        return self.name_en

    @property
    def name(self):
        """Display name in the active language (billing reads ``plan.name``)."""
        return self.display_name

    @property
    def display_description(self):
        if (get_language() or "").startswith("th") and self.description_th:
            return self.description_th
        return self.description_en

    def clean(self):
        from orvahr_saas.plans import MODULE_CATALOG

        unknown = sorted(set(self.modules or []) - set(MODULE_CATALOG))
        if unknown:
            raise ValidationError(
                {"modules": _("Unknown modules: %(m)s") % {"m": ", ".join(unknown)}}
            )

    def has_module(self, label):
        return label in (self.modules or [])


class Tenant(TenantMixin):
    """One customer = one PostgreSQL schema (``schema_name``)."""

    STATUS_ACTIVE = "active"
    STATUS_TRIAL = "trial"
    STATUS_SUSPENDED = "suspended"
    # Trial over and not converted: users can still sign in, but only the
    # account/billing pages answer (TenantMiddleware); data is kept.
    STATUS_TRIAL_EXPIRED = "trial_expired"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, _("Active")),
        (STATUS_TRIAL, _("Trial")),
        (STATUS_TRIAL_EXPIRED, _("Trial ended")),
        (STATUS_SUSPENDED, _("Suspended")),
    ]
    # Background jobs run only for these (horilla.tenancy.iter_schedulable_tenants).
    SCHEDULABLE_STATUSES = (STATUS_ACTIVE, STATUS_TRIAL)

    name = models.CharField(_("Name"), max_length=200)
    slug = models.SlugField(
        _("Slug"), max_length=41, unique=True, validators=[validate_slug]
    )
    status = models.CharField(
        _("Status"), max_length=20, choices=STATUS_CHOICES, default=STATUS_TRIAL
    )
    plan = models.ForeignKey(
        Plan,
        verbose_name=_("Plan"),
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="tenants",
    )
    # Mirror of plan.code, kept for create_tenant --plan and older scripts:
    # save() resolves a code given here into ``plan`` (see _sync_plan).
    plan_code = models.CharField(_("Plan code"), max_length=50, blank=True, default="")
    contact_email = models.EmailField(_("Contact email"), blank=True, default="")
    # Per-tenant override of plan.max_employees (empty = use the plan's).
    max_employees = models.PositiveIntegerField(
        _("Maximum employees"), null=True, blank=True
    )
    trial_ends_on = models.DateField(_("Trial ends on"), null=True, blank=True)
    trial_expired_at = models.DateTimeField(
        _("Trial expired at"), null=True, blank=True
    )
    suspended_at = models.DateTimeField(_("Suspended at"), null=True, blank=True)
    suspension_reason = models.CharField(
        _("Suspension reason"), max_length=255, blank=True, default=""
    )
    # Status to return to on resume (trial or active).
    status_before_suspension = models.CharField(max_length=20, blank=True, default="")
    created_on = models.DateTimeField(_("Created on"), auto_now_add=True)

    # Never drop a customer's schema because a row was deleted; dropping
    # data is an explicit, separate operation.
    auto_drop_schema = False
    auto_create_schema = True

    class Meta:
        ordering = ["schema_name"]

    def __str__(self):
        return f"{self.name} ({self.schema_name})"

    @property
    def is_suspended(self) -> bool:
        return self.status == self.STATUS_SUSPENDED

    @property
    def is_trial_expired(self) -> bool:
        return self.status == self.STATUS_TRIAL_EXPIRED

    @property
    def employee_limit(self):
        """Active-employee ceiling: tenant override, else the plan's, else None."""
        if self.max_employees is not None:
            return self.max_employees
        plan = self.plan if self.plan_id else None
        return plan.max_employees if plan is not None else None

    def _sync_plan(self):
        if self.plan_id:
            self.plan_code = self.plan.code
        elif self.plan_code:
            self.plan = Plan.objects.filter(code=self.plan_code).first()

    def save(self, *args, **kwargs):
        self._sync_plan()
        return super().save(*args, **kwargs)


class Domain(DomainMixin):
    """Host name that routes to a tenant, e.g. ``acme.orvahr.example``."""

    def __str__(self):
        return self.domain


# ---------------------------------------------------------------- sign-up


class TenantRequest(models.Model):
    """
    A sign-up / trial request from the public site. Never provisions by
    itself: an operator approves it in the console, then creates the tenant.
    """

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_PROVISIONING = "provisioning"
    STATUS_PROVISIONED = "provisioned"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_PENDING, _("Pending")),
        (STATUS_APPROVED, _("Approved")),
        (STATUS_REJECTED, _("Rejected")),
        (STATUS_PROVISIONING, _("Provisioning")),
        (STATUS_PROVISIONED, _("Provisioned")),
        (STATUS_FAILED, _("Provisioning failed")),
    ]
    # Requests in these states hold their sub-domain.
    HOLDS_SLUG = (STATUS_PENDING, STATUS_APPROVED, STATUS_PROVISIONING)

    company_name = models.CharField(_("Company name"), max_length=200)
    slug = models.SlugField(_("Sub-domain"), max_length=41)
    contact_name = models.CharField(_("Contact name"), max_length=150)
    contact_email = models.EmailField(_("Contact email"))
    contact_phone = models.CharField(_("Phone"), max_length=30)
    employee_count = models.PositiveIntegerField(_("Number of employees"))
    plan = models.ForeignKey(
        Plan,
        verbose_name=_("Plan"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="requests",
    )
    consent_to_contact = models.BooleanField(_("Consent to be contacted"))
    consent_at = models.DateTimeField(_("Consent given at"), null=True, blank=True)
    language = models.CharField(max_length=10, blank=True, default="")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    status = models.CharField(
        _("Status"), max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    created_at = models.DateTimeField(_("Created at"), auto_now_add=True)
    reviewed_by = models.ForeignKey(
        "ConsoleOperator",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    tenant = models.ForeignKey(
        Tenant, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    provision_log = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.company_name} ({self.slug})"


# --------------------------------------------------------------- console


class ConsoleOperator(models.Model):
    """
    Staff account for the operator console (/console/ on the public host).

    Not ``AUTH_USER_MODEL``: HorillaUser lives in each tenant schema and the
    public schema deliberately has no user table, so operators are a separate,
    tiny identity that can never log into a tenant (and vice versa). Created
    with ``manage.py create_console_operator``; the password must pass
    AUTH_PASSWORD_VALIDATORS (checked again at every login).
    """

    username = models.CharField(_("Username"), max_length=150, unique=True)
    email = models.EmailField(_("Email"), blank=True, default="")
    password = models.CharField(max_length=128)
    is_staff = models.BooleanField(_("Staff"), default=True)
    is_active = models.BooleanField(_("Active"), default=True)
    last_login = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    # Two-factor sign-in (orvahr_saas.totp): base32 RFC 6238 secret, set once
    # the operator has confirmed a code from their authenticator app.
    totp_secret = models.CharField(max_length=64, blank=True, default="")
    totp_enabled_at = models.DateTimeField(null=True, blank=True)
    # Last time step accepted, so a code cannot be replayed.
    totp_last_counter = models.BigIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["username"]

    @property
    def has_totp(self) -> bool:
        return bool(self.totp_secret and self.totp_enabled_at)

    def __str__(self):
        return self.username

    def set_password(self, raw_password):
        from django.contrib.auth.hashers import make_password

        self.password = make_password(raw_password)

    def check_password(self, raw_password):
        from django.contrib.auth.hashers import check_password

        def setter(raw):
            self.set_password(raw)
            type(self).objects.filter(pk=self.pk).update(password=self.password)

        return check_password(raw_password, self.password, setter)

    # validate_password's UserAttributeSimilarityValidator reads these.
    def get_username(self):
        return self.username


class OperatorRecoveryCode(models.Model):
    """One-time 2FA recovery code (salted SHA-256; the code is shown once)."""

    operator = models.ForeignKey(
        ConsoleOperator, on_delete=models.CASCADE, related_name="recovery_codes"
    )
    code_hash = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["pk"]


class OperatorAction(models.Model):
    """Audit trail of control-plane changes (who / when / what)."""

    operator = models.ForeignKey(
        ConsoleOperator,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="actions",
    )
    # Who, when the actor is not an operator (e.g. "billing", "system").
    actor_label = models.CharField(max_length=100, blank=True, default="")
    at = models.DateTimeField(default=timezone.now, db_index=True)
    action = models.CharField(max_length=50)
    tenant = models.ForeignKey(
        Tenant, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    tenant_request = models.ForeignKey(
        TenantRequest,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    detail = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-at"]

    def __str__(self):
        return f"{self.at:%Y-%m-%d %H:%M} {self.actor} {self.action}"

    @property
    def actor(self):
        if self.operator_id:
            return self.operator.username
        return self.actor_label or "system"


# ---------------------------------------------------------- notifications


class NotificationLog(models.Model):
    """
    One control-plane e-mail (orvahr_saas.notifications): who, what, when,
    outcome. Deliberately no subject or body -- the only personal datum kept
    is the recipient address.
    """

    STATUS_SENT = "sent"
    STATUS_FAILED = "failed"
    STATUS_SKIPPED = "skipped"
    STATUS_CHOICES = [
        (STATUS_SENT, _("Sent")),
        (STATUS_FAILED, _("Failed")),
        (STATUS_SKIPPED, _("Skipped")),
    ]

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    event = models.CharField(max_length=50, db_index=True)
    recipient = models.EmailField(max_length=254)
    # operator / tenant / applicant
    audience = models.CharField(max_length=20, blank=True, default="")
    language = models.CharField(max_length=10, blank=True, default="")
    tenant = models.ForeignKey(
        Tenant, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    tenant_request = models.ForeignKey(
        TenantRequest,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    # Reference to the billing document / payment etc. ("invoice:42").
    object_ref = models.CharField(max_length=100, blank=True, default="")
    # Same key + recipient already sent -> not sent again (scheduler re-runs).
    dedupe_key = models.CharField(max_length=150, blank=True, default="", db_index=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES)
    # Exception class and a short message; never the message content.
    error = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.event} -> {self.recipient}"
