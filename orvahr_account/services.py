"""
Operator-side hooks for what tenants submit on their subscription pages.

PaymentNotice and PlanChangeRequest rows live in each TENANT schema (a tenant
app cannot write to the public schema's billing tables). The operator UI on
the public host (/billing/, /console/) reads and settles them through these
functions, which switch into each tenant's schema with ``schema_context``.
They are meant to be called with the connection on the public schema, e.g.
from an ``orvahr_billing`` or ``orvahr_saas.console`` view.

Reading::

    from orvahr_account.services import (
        pending_payment_notices, pending_plan_change_requests,
    )
    for notice in pending_payment_notices():          # all tenants
        notice.tenant, notice.token, notice.invoice_number, notice.amount, ...
    pending_payment_notices(tenant=t)                 # one tenant

Settling a payment notice (records an orvahr_billing Payment with the slip,
which issues the tax invoice/receipt, and marks the notice confirmed)::

    payment = confirm_payment_notice(tenant, token, by=operator.username,
                                     bank_account=bank_account_or_None)
    reject_payment_notice(tenant, token, by=..., reason="Amount not received")

Slip for the operator (bytes, file name, content type)::

    data, name, content_type = open_payment_slip(tenant, token)

Plan change requests::

    decide_plan_change_request(tenant, token, approve=True, by=...,
                               note="", apply=False)

``apply=True`` also moves the tenant to the requested plan with
``orvahr_saas.services.change_plan`` (clearing any per-tenant employee
override). The billing ``Subscription.plan_code`` / price is NOT changed here;
the operator adjusts it in /billing/tenants/<id>/ as for any plan change.

A tenant whose schema lacks the tables (not yet migrated) is skipped and
logged, never fatal for the list.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.core.files.base import ContentFile
from django.db import DatabaseError, transaction
from django.utils import timezone
from horilla.tenancy import public_schema_name
from horilla.tenancy import use_schema as schema_context

logger = logging.getLogger(__name__)


class NoticeError(Exception):
    """The notice / request cannot be settled (already decided, invoice gone...)."""


@dataclass
class PendingPaymentNotice:
    tenant: object
    token: str
    invoice_number: str
    amount: object
    wht_amount: object
    wht_certificate_no: str
    transfer_date: object
    reference: str
    note: str
    submitted_at: object
    submitted_by: str
    slip_content_type: str
    extra: dict = field(default_factory=dict)

    @property
    def gross_amount(self):
        return (self.amount or 0) + (self.wht_amount or 0)


@dataclass
class PendingPlanChangeRequest:
    tenant: object
    token: str
    current_plan_code: str
    requested_plan_code: str
    note: str
    requested_at: object
    requested_by: str


def _tenants(tenant=None):
    from orvahr_saas.models import Tenant

    if tenant is not None:
        return [tenant]
    with schema_context(public_schema_name()):
        return list(
            Tenant.objects.exclude(schema_name=public_schema_name()).order_by(
                "schema_name"
            )
        )


def _user_label(user):
    if user is None:
        return ""
    return getattr(user, "get_username", lambda: "")() or str(user)


def _collect(build, tenant=None):
    rows = []
    for current in _tenants(tenant):
        try:
            with schema_context(current.schema_name), transaction.atomic():
                rows.extend(build(current))
        except DatabaseError:
            logger.exception(
                "Could not read subscription requests of %s", current.schema_name
            )
    return rows


def pending_payment_notices(tenant=None):
    """Every PENDING payment notice (all tenants, or one), oldest first."""
    from orvahr_account.models import PaymentNotice

    def build(current):
        qs = (
            PaymentNotice.objects.filter(status=PaymentNotice.STATUS_PENDING)
            .select_related("submitted_by")
            .order_by("submitted_at", "pk")
        )
        return [
            PendingPaymentNotice(
                tenant=current,
                token=str(n.token),
                invoice_number=n.invoice_number,
                amount=n.amount,
                wht_amount=n.wht_amount,
                wht_certificate_no=n.wht_certificate_no,
                transfer_date=n.transfer_date,
                reference=n.reference,
                note=n.note,
                submitted_at=n.submitted_at,
                submitted_by=_user_label(n.submitted_by),
                slip_content_type=n.slip_content_type,
            )
            for n in qs
        ]

    return _collect(build, tenant)


def pending_plan_change_requests(tenant=None):
    """Every PENDING plan change request (all tenants, or one), oldest first."""
    from orvahr_account.models import PlanChangeRequest

    def build(current):
        qs = (
            PlanChangeRequest.objects.filter(status=PlanChangeRequest.STATUS_PENDING)
            .select_related("requested_by")
            .order_by("requested_at", "pk")
        )
        return [
            PendingPlanChangeRequest(
                tenant=current,
                token=str(r.token),
                current_plan_code=r.current_plan_code,
                requested_plan_code=r.requested_plan_code,
                note=r.note,
                requested_at=r.requested_at,
                requested_by=_user_label(r.requested_by),
            )
            for r in qs
        ]

    return _collect(build, tenant)


# ------------------------------------------------------------------ actions


def _pending_notice(token, lock=True):
    from orvahr_account.models import PaymentNotice

    qs = PaymentNotice.objects.filter(token=token)
    if lock:
        qs = qs.select_for_update()
    notice = qs.first()
    if notice is None:
        raise NoticeError("Payment notice not found.")
    if notice.status != PaymentNotice.STATUS_PENDING:
        raise NoticeError("This payment notice has already been handled.")
    return notice


def open_payment_slip(tenant, token):
    """``(bytes, file name, content type)`` of a notice's slip, for operators."""
    from orvahr_account.models import PaymentNotice

    with schema_context(tenant.schema_name):
        notice = PaymentNotice.objects.filter(token=token).first()
        if notice is None or not notice.slip:
            raise NoticeError("Slip not found.")
        with notice.slip.open("rb") as handle:
            data = handle.read()
        name = notice.slip.name.rsplit("/", 1)[-1]
        return data, name, notice.slip_content_type or "application/octet-stream"


def confirm_payment_notice(
    tenant, token, *, by, bank_account=None, payment_date=None, note=""
):
    """
    Record the notified transfer as an orvahr_billing Payment against
    ``tenant``'s invoice (which issues the tax invoice/receipt), copying the
    slip into billing's private storage, and mark the notice confirmed.
    Raises ``NoticeError`` or ``orvahr_billing.services.BillingError``.
    """
    from orvahr_billing import services as billing
    from orvahr_billing.models import BillingDocument, DocStatus, DocType

    with transaction.atomic():
        with schema_context(tenant.schema_name):
            notice = _pending_notice(token)
            slip = None
            if notice.slip:
                with notice.slip.open("rb") as handle:
                    slip = ContentFile(
                        handle.read(), name=notice.slip.name.rsplit("/", 1)[-1]
                    )
        with schema_context(public_schema_name()):
            invoice = BillingDocument.objects.filter(
                tenant_id=tenant.pk,
                number=notice.invoice_number,
                doc_type=DocType.INVOICE,
                status=DocStatus.ISSUED,
            ).first()
            if invoice is None:
                raise NoticeError("The invoice of this notice does not exist.")
            payment = billing.record_payment(
                invoice,
                payment_date=payment_date or notice.transfer_date,
                amount_received=notice.amount,
                wht_amount=notice.wht_amount,
                wht_certificate_no=notice.wht_certificate_no,
                reference=notice.reference,
                bank_account=bank_account,
                slip=slip,
                note=note or f"Payment notice {notice.token}",
                by=by,
            )
        with schema_context(tenant.schema_name):
            type(notice).objects.filter(pk=notice.pk).update(
                status=notice.STATUS_CONFIRMED,
                reviewed_at=timezone.now(),
                reviewed_by=by,
                review_note=note,
                receipt_number=payment.receipt.number if payment.receipt_id else "",
            )
    return payment


def reject_payment_notice(tenant, token, *, by, reason=""):
    with schema_context(tenant.schema_name), transaction.atomic():
        notice = _pending_notice(token)
        notice.status = notice.STATUS_REJECTED
        notice.reviewed_at = timezone.now()
        notice.reviewed_by = by
        notice.review_note = reason
        notice.save(
            update_fields=["status", "reviewed_at", "reviewed_by", "review_note"]
        )
    return notice


def decide_plan_change_request(tenant, token, *, approve, by, note="", apply=False):
    from orvahr_account.models import PlanChangeRequest

    with transaction.atomic():
        with schema_context(tenant.schema_name):
            request = (
                PlanChangeRequest.objects.select_for_update()
                .filter(token=token)
                .first()
            )
            if request is None:
                raise NoticeError("Plan change request not found.")
            if request.status != PlanChangeRequest.STATUS_PENDING:
                raise NoticeError("This request has already been handled.")
            request.status = (
                PlanChangeRequest.STATUS_APPROVED
                if approve
                else PlanChangeRequest.STATUS_REJECTED
            )
            request.decided_at = timezone.now()
            request.decided_by = by
            request.decision_note = note
            request.save(
                update_fields=["status", "decided_at", "decided_by", "decision_note"]
            )
        if approve and apply:
            from orvahr_saas.models import Plan
            from orvahr_saas.services import change_plan

            with schema_context(public_schema_name()):
                plan = Plan.objects.filter(code=request.requested_plan_code).first()
                if plan is None:
                    raise NoticeError("The requested plan no longer exists.")
                change_plan(tenant, plan, max_employees=None, actor_label=by)
    return request
