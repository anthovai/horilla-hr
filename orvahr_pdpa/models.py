"""
PDPA (Thai Personal Data Protection Act B.E. 2562) tooling.

Nothing in this app makes an installation compliant. Notice texts, deadlines
and retention periods are configurable data that must be reviewed by counsel;
the defaults below are placeholders marked "verify".

Two kinds of model live here:

* Configuration and case records (notices, purposes, settings, requests,
  breaches, retention policies) extend HorillaModel and are company scoped.
* Evidence logs (ConsentRecord, SensitiveAccessLog) are plain append-only
  models: HorillaModel would add an audit-history field and the XSS write
  hook, and neither is wanted on a high-volume evidence table. They store
  field *names* and identifiers only, never personal values.
"""

from datetime import timedelta

from django.apps import apps
from django.conf import settings
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from base.horilla_company_manager import HorillaCompanyManager
from horilla.models import HorillaModel

# Placeholders. NEEDS COUNSEL REVIEW before relying on them.
DEFAULT_DSR_DUE_DAYS = 30  # verify: PDPA s.30 response period
DEFAULT_BREACH_DEADLINE_HOURS = 72  # verify: PDPA s.37(4) notification window

AUDIENCE_CHOICES = [
    ("employee", _("Employees")),
    ("candidate", _("Candidates")),
]


def employee_company(employee):
    work_info = getattr(employee, "employee_work_info", None) if employee else None
    return getattr(work_info, "company_id", None) if work_info else None


class PdpaSettings(HorillaModel):
    """DPO contact details and deadline settings, one row per company.

    The row with no company is the default for companies without their own.
    """

    company = models.OneToOneField(
        "base.Company",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="pdpa_settings",
        verbose_name=_("Company"),
        help_text=_("Leave empty for the default used by companies without their own settings."),
    )
    dpo_name = models.CharField(max_length=150, blank=True, verbose_name=_("Data Protection Officer"))
    dpo_email = models.EmailField(blank=True, verbose_name=_("DPO email"))
    dpo_phone = models.CharField(max_length=30, blank=True, verbose_name=_("DPO phone"))
    dpo_address = models.TextField(blank=True, verbose_name=_("DPO contact address"))
    dsr_due_days = models.PositiveSmallIntegerField(
        default=DEFAULT_DSR_DUE_DAYS,
        verbose_name=_("Data subject request due (days)"),
        help_text=_("Days from receipt to respond. Default 30; have counsel verify."),
    )
    breach_deadline_hours = models.PositiveSmallIntegerField(
        default=DEFAULT_BREACH_DEADLINE_HOURS,
        verbose_name=_("Breach notification deadline (hours)"),
        help_text=_("Hours from detection to notify the regulator. Default 72; have counsel verify."),
    )

    objects = HorillaCompanyManager("company")
    all_objects = models.Manager()

    class Meta:
        verbose_name = _("PDPA Settings")
        verbose_name_plural = _("PDPA Settings")
        permissions = [
            ("pdpa_officer", _("Can act as PDPA officer")),
            ("export_personal_data", _("Can export a data subject's personal data")),
            ("anonymise_personal_data", _("Can anonymise records past retention")),
        ]

    def __str__(self):
        return str(self.company or _("Default"))

    @classmethod
    def for_company(cls, company):
        qs = cls.all_objects.all()
        row = qs.filter(company=company).first() if company else None
        return row or qs.filter(company__isnull=True).first()

    @classmethod
    def dsr_days_for(cls, company):
        row = cls.for_company(company)
        return row.dsr_due_days if row else DEFAULT_DSR_DUE_DAYS

    @classmethod
    def breach_hours_for(cls, company):
        row = cls.for_company(company)
        return row.breach_deadline_hours if row else DEFAULT_BREACH_DEADLINE_HOURS


class PrivacyNotice(HorillaModel):
    """A versioned privacy notice. Text is legal content: have counsel review."""

    company = models.ForeignKey(
        "base.Company",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="privacy_notices",
        verbose_name=_("Company"),
        help_text=_("Leave empty to use this notice for all companies."),
    )
    audience = models.CharField(max_length=20, choices=AUDIENCE_CHOICES, verbose_name=_("Audience"))
    version = models.CharField(max_length=20, verbose_name=_("Version"))
    title = models.CharField(max_length=200, verbose_name=_("Title"))
    text_th = models.TextField(verbose_name=_("Notice text (Thai)"))
    text_en = models.TextField(blank=True, verbose_name=_("Notice text (English)"))
    effective_from = models.DateField(verbose_name=_("Effective from"))
    published = models.BooleanField(default=False, verbose_name=_("Published"))

    objects = HorillaCompanyManager("company")
    all_objects = models.Manager()

    class Meta:
        verbose_name = _("Privacy Notice")
        verbose_name_plural = _("Privacy Notices")
        ordering = ["audience", "-effective_from", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "audience", "version"], name="pdpa_unique_notice_version"
            )
        ]

    def __str__(self):
        return f"{self.title} v{self.version}"

    @classmethod
    def current(cls, audience, company=None, on=None):
        """The published notice in force on ``on`` (company's own, else global)."""
        on = on or timezone.localdate()
        qs = cls.all_objects.filter(
            audience=audience, published=True, is_active=True, effective_from__lte=on
        ).order_by("-effective_from", "-id")
        notice = qs.filter(company=company).first() if company else None
        return notice or qs.filter(company__isnull=True).first()


class ConsentPurpose(HorillaModel):
    """A processing purpose that relies on consent (e.g. marketing photos)."""

    company = models.ForeignKey(
        "base.Company",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="pdpa_consent_purposes",
        verbose_name=_("Company"),
    )
    audience = models.CharField(max_length=20, choices=AUDIENCE_CHOICES, verbose_name=_("Audience"))
    code = models.SlugField(max_length=50, verbose_name=_("Code"))
    name = models.CharField(max_length=150, verbose_name=_("Name (English)"))
    name_th = models.CharField(max_length=150, blank=True, verbose_name=_("Name (Thai)"))
    description = models.TextField(blank=True, verbose_name=_("Description"))
    optional = models.BooleanField(
        default=True,
        verbose_name=_("Optional"),
        help_text=_("Optional purposes can be given and withdrawn by the data subject at any time."),
    )

    objects = HorillaCompanyManager("company")
    all_objects = models.Manager()

    class Meta:
        verbose_name = _("Consent Purpose")
        verbose_name_plural = _("Consent Purposes")
        ordering = ["audience", "name"]
        constraints = [
            models.UniqueConstraint(fields=["company", "audience", "code"], name="pdpa_unique_purpose")
        ]

    def __str__(self):
        return self.name

    @classmethod
    def for_subject(cls, audience, company):
        qs = cls.all_objects.filter(audience=audience, is_active=True)
        if company:
            return qs.filter(models.Q(company=company) | models.Q(company__isnull=True))
        return qs.filter(company__isnull=True)


class ConsentRecord(models.Model):
    """
    Append-only consent evidence. The current state of a purpose for a subject
    is its latest row. ``purpose`` empty means acknowledgement of the notice.
    """

    ACTION_CHOICES = [
        ("acknowledged", _("Acknowledged notice")),
        ("granted", _("Granted")),
        ("withdrawn", _("Withdrawn")),
    ]
    CHANNEL_CHOICES = [
        ("web", _("Web self-service")),
        ("login", _("Login")),
        ("candidate_apply", _("Candidate application")),
        ("hr", _("Recorded by HR")),
        ("paper", _("Paper form")),
        ("line", _("LINE")),
    ]

    company = models.ForeignKey("base.Company", on_delete=models.SET_NULL, null=True, blank=True)
    employee = models.ForeignKey(
        "employee.Employee",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="pdpa_consents",
    )
    candidate = models.ForeignKey(
        "recruitment.Candidate",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="pdpa_consents",
    )
    notice = models.ForeignKey(
        PrivacyNotice, on_delete=models.PROTECT, null=True, blank=True, related_name="consents"
    )
    notice_version = models.CharField(max_length=20, blank=True, verbose_name=_("Notice version"))
    purpose = models.ForeignKey(
        ConsentPurpose, on_delete=models.PROTECT, null=True, blank=True, related_name="records"
    )
    action = models.CharField(max_length=20, choices=ACTION_CHOICES, verbose_name=_("Action"))
    channel = models.CharField(max_length=20, choices=CHANNEL_CHOICES, default="web")
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    objects = HorillaCompanyManager("company")
    all_objects = models.Manager()

    class Meta:
        verbose_name = _("Consent Record")
        verbose_name_plural = _("Consent Records")
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(employee__isnull=False, candidate__isnull=True)
                    | models.Q(employee__isnull=True, candidate__isnull=False)
                ),
                name="pdpa_consent_one_subject",
            )
        ]

    def __str__(self):
        return f"{self.get_action_display()} {self.purpose or self.notice_version}"

    @property
    def granted(self):
        return self.action in ("granted", "acknowledged")


class DataSubjectRequest(HorillaModel):
    TYPE_CHOICES = [
        ("access", _("Access")),
        ("rectification", _("Rectification")),
        ("erasure", _("Erasure")),
        ("restriction", _("Restriction of processing")),
        ("objection", _("Objection")),
        ("portability", _("Data portability")),
        ("withdraw", _("Withdraw consent")),
    ]
    STATUS_CHOICES = [
        ("received", _("Received")),
        ("verifying", _("Verifying identity")),
        ("in_progress", _("In progress")),
        ("completed", _("Completed")),
        ("rejected", _("Rejected")),
    ]
    OPEN_STATUSES = ("received", "verifying", "in_progress")
    TRANSITIONS = {
        "received": ("verifying", "in_progress", "rejected"),
        "verifying": ("in_progress", "rejected"),
        "in_progress": ("completed", "rejected"),
        "completed": (),
        "rejected": (),
    }

    company = models.ForeignKey(
        "base.Company", on_delete=models.CASCADE, null=True, blank=True, verbose_name=_("Company")
    )
    employee = models.ForeignKey(
        "employee.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pdpa_requests",
        verbose_name=_("Employee"),
    )
    candidate = models.ForeignKey(
        "recruitment.Candidate",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pdpa_requests",
        verbose_name=_("Candidate"),
    )
    subject_name = models.CharField(
        max_length=200,
        blank=True,
        verbose_name=_("Subject name"),
        help_text=_("For a requester who is neither an employee nor a candidate."),
    )
    request_type = models.CharField(max_length=20, choices=TYPE_CHOICES, verbose_name=_("Request type"))
    received_at = models.DateTimeField(default=timezone.now, verbose_name=_("Received at"))
    due_at = models.DateTimeField(
        blank=True,
        verbose_name=_("Due at"),
        help_text=_("Filled from the company's PDPA settings when left empty."),
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="received", verbose_name=_("Status"))
    assignee = models.ForeignKey(
        "employee.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pdpa_assigned_requests",
        verbose_name=_("Assignee"),
    )
    details = models.TextField(blank=True, verbose_name=_("Request details"))
    notes = models.TextField(blank=True, verbose_name=_("Internal notes"))
    resolution = models.TextField(blank=True, verbose_name=_("Resolution"))
    closed_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Closed at"))

    objects = HorillaCompanyManager("company")
    all_objects = models.Manager()

    class Meta:
        verbose_name = _("Data Subject Request")
        verbose_name_plural = _("Data Subject Requests")
        ordering = ["due_at", "id"]

    def __str__(self):
        return f"DSR-{self.pk} {self.get_request_type_display()}"

    @property
    def subject_display(self):
        if self.employee_id:
            return str(self.employee)
        if self.candidate_id:
            return str(self.candidate)
        return self.subject_name

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES

    @property
    def is_overdue(self):
        return self.is_open and self.due_at is not None and self.due_at < timezone.now()

    def allowed_next(self):
        return self.TRANSITIONS.get(self.status, ())

    def clean(self):
        super().clean()
        if not (self.employee_id or self.candidate_id or self.subject_name):
            raise ValidationError(_("Choose an employee or a candidate, or enter the subject's name."))

    def save(self, *args, **kwargs):
        if self.company_id is None and self.employee_id:
            self.company = employee_company(self.employee)
        if self.company_id is None and self.candidate_id:
            recruitment = getattr(self.candidate, "recruitment_id", None)
            self.company = getattr(recruitment, "company_id", None)
        if not self.due_at:
            received = self.received_at or timezone.now()
            self.due_at = received + timedelta(days=PdpaSettings.dsr_days_for(self.company))
        if self.status in self.OPEN_STATUSES:
            self.closed_at = None
        elif self.closed_at is None:
            self.closed_at = timezone.now()
        super().save(*args, **kwargs)


class BreachIncident(HorillaModel):
    RISK_CHOICES = [
        ("low", _("Low (unlikely to result in risk)")),
        ("medium", _("Risk to rights and freedoms")),
        ("high", _("High risk")),
    ]
    STATUS_CHOICES = [
        ("open", _("Open")),
        ("contained", _("Contained")),
        ("closed", _("Closed")),
    ]

    company = models.ForeignKey(
        "base.Company", on_delete=models.CASCADE, null=True, blank=True, verbose_name=_("Company")
    )
    title = models.CharField(max_length=200, verbose_name=_("Title"))
    description = models.TextField(verbose_name=_("Description"))
    detected_at = models.DateTimeField(default=timezone.now, verbose_name=_("Detected at"))
    data_categories = models.CharField(
        max_length=255, blank=True, verbose_name=_("Data categories affected")
    )
    affected_subjects_count = models.PositiveIntegerField(default=0, verbose_name=_("Affected data subjects"))
    risk_level = models.CharField(max_length=10, choices=RISK_CHOICES, default="medium", verbose_name=_("Risk level"))
    deadline_at = models.DateTimeField(
        blank=True,
        verbose_name=_("Regulator notification deadline"),
        help_text=_("Filled from the company's PDPA settings when left empty."),
    )
    reported_to_pdpc_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Reported to PDPC at"))
    subjects_notified_at = models.DateTimeField(null=True, blank=True, verbose_name=_("Data subjects notified at"))
    actions_taken = models.TextField(blank=True, verbose_name=_("Actions taken"))
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="open", verbose_name=_("Status"))

    objects = HorillaCompanyManager("company")
    all_objects = models.Manager()

    class Meta:
        verbose_name = _("Breach Incident")
        verbose_name_plural = _("Breach Incidents")
        ordering = ["-detected_at", "-id"]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.deadline_at:
            self.deadline_at = (self.detected_at or timezone.now()) + timedelta(
                hours=PdpaSettings.breach_hours_for(self.company)
            )
        super().save(*args, **kwargs)

    @property
    def awaiting_report(self):
        return self.reported_to_pdpc_at is None and self.risk_level != "low"

    @property
    def seconds_left(self):
        return int((self.deadline_at - timezone.now()).total_seconds())

    @property
    def is_late(self):
        if self.reported_to_pdpc_at:
            return self.reported_to_pdpc_at > self.deadline_at
        return self.awaiting_report and self.seconds_left < 0


class RetentionPolicy(HorillaModel):
    ACTION_CHOICES = [
        ("flag", _("Flag for review")),
        ("anonymise", _("Anonymise")),
        ("delete", _("Delete (manual, outside this tool)")),
    ]
    # Anonymisation is implemented for these model labels only.
    ANONYMISABLE = ("employee.employee",)

    company = models.ForeignKey(
        "base.Company", on_delete=models.CASCADE, null=True, blank=True, verbose_name=_("Company")
    )
    name = models.CharField(max_length=150, verbose_name=_("Name"))
    model_label = models.CharField(
        max_length=100,
        verbose_name=_("Model"),
        help_text=_("app_label.ModelName, e.g. employee.Employee or recruitment.Candidate"),
    )
    date_field = models.CharField(
        max_length=150,
        verbose_name=_("Date field"),
        help_text=_("Date the retention period counts from, e.g. employee_work_info__contract_end_date"),
    )
    keep_years = models.PositiveSmallIntegerField(verbose_name=_("Keep for (years)"))
    action = models.CharField(max_length=20, choices=ACTION_CHOICES, default="flag", verbose_name=_("Action"))
    notes = models.TextField(
        blank=True,
        verbose_name=_("Legal basis / notes"),
        help_text=_("Record the legal basis for the period. Have counsel review."),
    )

    objects = HorillaCompanyManager("company")
    all_objects = models.Manager()

    class Meta:
        verbose_name = _("Retention Policy")
        verbose_name_plural = _("Retention Policies")
        ordering = ["name"]

    def __str__(self):
        return self.name

    def get_model(self):
        try:
            return apps.get_model(self.model_label)
        except (LookupError, ValueError):
            return None

    @property
    def normalised_label(self):
        model = self.get_model()
        return model._meta.label_lower if model else ""

    @property
    def can_anonymise(self):
        return self.action == "anonymise" and self.normalised_label in self.ANONYMISABLE

    def resolve_date_field(self):
        """Walk ``date_field`` (may cross relations) and return the final field."""
        model = self.get_model()
        if model is None:
            raise ValidationError({"model_label": _("Unknown model.")})
        field = None
        parts = self.date_field.split("__")
        for index, part in enumerate(parts):
            try:
                field = model._meta.get_field(part)
            except FieldDoesNotExist:
                raise ValidationError({"date_field": _("Unknown field.")})
            if index < len(parts) - 1:
                if not field.is_relation or field.related_model is None:
                    raise ValidationError({"date_field": _("Unknown field.")})
                model = field.related_model
        if not isinstance(field, (models.DateField, models.DateTimeField)):
            raise ValidationError({"date_field": _("Must be a date or date-time field.")})
        return field

    def clean(self):
        super().clean()
        if self.model_label:
            self.resolve_date_field()
        if self.action == "anonymise" and self.normalised_label not in self.ANONYMISABLE:
            raise ValidationError(
                {"action": _("Anonymisation is only available for employee records.")}
            )


class SensitiveAccessLog(models.Model):
    """Who viewed which sensitive fields of whom. Field names only, never values."""

    company = models.ForeignKey("base.Company", on_delete=models.SET_NULL, null=True, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("User"),
    )
    username = models.CharField(max_length=150, blank=True)
    subject_employee = models.ForeignKey(
        "employee.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        verbose_name=_("Subject"),
    )
    subject_label = models.CharField(max_length=200, blank=True)
    fields = models.JSONField(default=list, verbose_name=_("Fields viewed"))
    context = models.CharField(max_length=255, blank=True, verbose_name=_("Context"))
    ip_address = models.GenericIPAddressField(null=True, blank=True, verbose_name=_("IP address"))
    accessed_at = models.DateTimeField(default=timezone.now, db_index=True, verbose_name=_("Accessed at"))

    objects = HorillaCompanyManager("company")
    all_objects = models.Manager()

    class Meta:
        verbose_name = _("Sensitive Access Log")
        verbose_name_plural = _("Sensitive Access Logs")
        ordering = ["-accessed_at", "-id"]

    def __str__(self):
        return f"{self.username} -> {self.subject_label} @ {self.accessed_at:%Y-%m-%d %H:%M}"
