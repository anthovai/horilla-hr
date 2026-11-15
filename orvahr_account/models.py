"""
Tenant-side records of the subscription pages (tables in each TENANT schema).

orvahr_billing lives in the public schema and a tenant app cannot add tables
there, so what a tenant *asks for* -- "I paid this invoice, here is the slip"
and "move us to another plan" -- is stored here, in the tenant's own schema,
and the operator side reads it by iterating tenants
(``orvahr_account.services.pending_payment_notices()`` /
``pending_plan_change_requests()``). Nothing here has a foreign key to a
public table: invoices and plans are referenced by their public number / code
and always re-resolved against the current tenant.

Both models are addressed by a random ``token`` in URLs, never by their id.
"""

import os
import uuid

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from orvahr_account.storage import private_storage

MANAGE_PERMISSION = "orvahr_account.manage_subscription"


def slip_upload_to(instance, filename):
    ext = os.path.splitext(filename)[1].lower()[:10]
    return f"slips/{timezone.now():%Y/%m}/{uuid.uuid4().hex}{ext}"


class PaymentNotice(models.Model):
    """A tenant's "we have transferred the money" notice, waiting for an operator."""

    STATUS_PENDING = "pending"
    STATUS_CONFIRMED = "confirmed"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_PENDING, _("Pending")),
        (STATUS_CONFIRMED, _("Confirmed")),
        (STATUS_REJECTED, _("Rejected")),
    ]

    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    invoice_number = models.CharField(_("Invoice"), max_length=30, db_index=True)
    amount = models.DecimalField(
        _("Amount transferred (THB)"),
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(0)],
    )
    transfer_date = models.DateField(_("Transfer date"))
    reference = models.CharField(_("Transfer reference"), max_length=255, blank=True)
    wht_amount = models.DecimalField(
        _("Withholding tax deducted (THB)"),
        max_digits=14,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
    )
    wht_certificate_no = models.CharField(
        _("WHT certificate (50 ทวิ) no."), max_length=100, blank=True
    )
    slip = models.FileField(
        _("Transfer slip"), upload_to=slip_upload_to, storage=private_storage
    )
    slip_content_type = models.CharField(max_length=50, blank=True)
    note = models.TextField(_("Note"), blank=True)
    status = models.CharField(
        _("Status"), max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    submitted_at = models.DateTimeField(_("Submitted at"), auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.CharField(max_length=150, blank=True)
    review_note = models.TextField(_("Operator note"), blank=True)
    receipt_number = models.CharField(_("Receipt"), max_length=30, blank=True)

    class Meta:
        ordering = ["-submitted_at", "-pk"]
        verbose_name = _("Payment notice")
        verbose_name_plural = _("Payment notices")
        permissions = [
            ("manage_subscription", _("Manage subscription and billing")),
        ]

    def __str__(self):
        return f"{self.invoice_number} {self.amount} ({self.status})"

    @property
    def gross_amount(self):
        return (self.amount or 0) + (self.wht_amount or 0)

    @property
    def is_pending(self):
        return self.status == self.STATUS_PENDING


class PlanChangeRequest(models.Model):
    """A tenant admin's request to move to another public plan."""

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_PENDING, _("Pending")),
        (STATUS_APPROVED, _("Approved")),
        (STATUS_REJECTED, _("Rejected")),
        (STATUS_CANCELLED, _("Cancelled")),
    ]

    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    current_plan_code = models.CharField(_("Current plan"), max_length=50, blank=True)
    requested_plan_code = models.CharField(_("Requested plan"), max_length=50)
    note = models.TextField(_("Note"), blank=True)
    status = models.CharField(
        _("Status"), max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    requested_at = models.DateTimeField(_("Requested at"), auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.CharField(max_length=150, blank=True)
    decision_note = models.TextField(_("Operator note"), blank=True)

    class Meta:
        ordering = ["-requested_at", "-pk"]
        verbose_name = _("Plan change request")
        verbose_name_plural = _("Plan change requests")

    def __str__(self):
        return f"{self.current_plan_code} -> {self.requested_plan_code} ({self.status})"
