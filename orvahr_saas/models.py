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


class Tenant(TenantMixin):
    """One customer = one PostgreSQL schema (``schema_name``)."""

    STATUS_ACTIVE = "active"
    STATUS_TRIAL = "trial"
    STATUS_SUSPENDED = "suspended"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, _("Active")),
        (STATUS_TRIAL, _("Trial")),
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
    # Placeholder until billing exists: free text such as "starter".
    plan_code = models.CharField(_("Plan"), max_length=50, blank=True, default="")
    contact_email = models.EmailField(_("Contact email"), blank=True, default="")
    max_employees = models.PositiveIntegerField(
        _("Maximum employees"), null=True, blank=True
    )
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


class Domain(DomainMixin):
    """Host name that routes to a tenant, e.g. ``acme.orvahr.example``."""

    def __str__(self):
        return self.domain
