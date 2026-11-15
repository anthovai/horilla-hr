"""
Billing operations. Views and management commands call these; nothing else
writes billing documents.

All functions that issue a document run in one transaction with the number
reservation (see ``numbering``), so a failure leaves neither a document nor
a gap in the series.
"""

import calendar
import datetime
import logging
import os

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max, Sum
from django.utils import timezone
from django.utils.translation import gettext as _

from orvahr_billing import integration
from orvahr_billing.models import (
    BankAccount,
    BillingCycle,
    BillingDocument,
    BillingLine,
    BillingProfile,
    DocStatus,
    DocType,
    Payment,
    PaymentStatus,
    PlanPrice,
    SellerSettings,
    Subscription,
    SubscriptionStatus,
)
from orvahr_billing.money import ZERO, line_amount, q, split_vat_inclusive, vat_on
from orvahr_billing.numbering import next_number
from orvahr_th.formatting import baht_text

logger = logging.getLogger(__name__)


class BillingError(Exception):
    """A billing rule refused the operation; the message is for the operator."""


# ------------------------------------------------------------------ helpers


def _today():
    return timezone.localdate()


def _add_lines(doc, lines):
    for position, line in enumerate(lines, start=1):
        quantity = q(line.get("quantity", 1))
        unit_price = q(line.get("unit_price", 0))
        BillingLine.objects.create(
            document=doc,
            position=position,
            description=line["description"],
            quantity=quantity,
            unit=line.get("unit", ""),
            unit_price=unit_price,
            amount=(
                q(line["amount"])
                if "amount" in line
                else line_amount(quantity, unit_price)
            ),
        )


def _snapshot_seller(doc, seller):
    doc.seller_name_th = seller.legal_name_th
    doc.seller_name_en = seller.legal_name_en
    doc.seller_tax_id = seller.tax_id
    doc.seller_branch_code = seller.branch_code
    doc.seller_address_th = seller.address_th
    doc.seller_address_en = seller.address_en
    doc.seller_phone = seller.phone
    doc.seller_email = seller.email
    doc.footer = seller.invoice_footer
    doc.bank_accounts = [
        account.as_snapshot() for account in BankAccount.objects.filter(is_active=True)
    ]


def _snapshot_buyer(doc):
    profile = BillingProfile.objects.filter(tenant=doc.tenant).first()
    if profile is None:
        raise BillingError(
            _("%(tenant)s has no billing profile (legal name, address, tax ID).")
            % {"tenant": doc.tenant}
        )
    doc.buyer_name = profile.legal_name
    doc.buyer_address = profile.address
    doc.buyer_vat_registered = profile.is_vat_registered
    # Notification 199: tax ID and establishment only for a VAT registrant.
    doc.buyer_tax_id = profile.tax_id or ""
    doc.buyer_branch_code = profile.branch_code if profile.is_vat_registered else ""
    doc.buyer_email = profile.email


def _compute_totals(doc):
    subtotal = sum((line.amount for line in doc.lines.all()), ZERO)
    doc.subtotal = q(subtotal)
    doc.vat_amount = (
        vat_on(doc.subtotal, doc.vat_rate) if doc.seller_vat_registered else ZERO
    )
    doc.total = doc.subtotal + doc.vat_amount
    doc.amount_in_words = baht_text(doc.total)


# --------------------------------------------------------------- documents


def create_draft(
    tenant,
    doc_type,
    lines,
    *,
    issue_date=None,
    due_date=None,
    period_start=None,
    period_end=None,
    subscription=None,
    related_document=None,
    notes="",
    by="",
    **extra,
):
    """A draft document with ``lines`` ([{description, quantity, unit, unit_price}])."""
    if not lines:
        raise BillingError(_("A document needs at least one line."))
    with transaction.atomic():
        doc = BillingDocument.objects.create(
            doc_type=doc_type,
            tenant=tenant,
            issue_date=issue_date or _today(),
            due_date=due_date,
            period_start=period_start,
            period_end=period_end,
            subscription=subscription,
            related_document=related_document,
            notes=notes,
            created_by=by,
            **extra,
        )
        _add_lines(doc, lines)
        seller = SellerSettings.load()
        doc.seller_vat_registered = seller.vat_registered
        doc.vat_rate = seller.vat_rate if seller.vat_registered else ZERO
        _compute_totals(doc)
        doc.save()
    return doc


def create_invoice(tenant, lines, **kwargs):
    return create_draft(tenant, DocType.INVOICE, lines, **kwargs)


def issue(doc, by="", *, vat_rate=None, vat_registered=None, vat_amount=None):
    """
    Give a draft its number, freeze seller/buyer details and totals.

    ``vat_rate`` / ``vat_registered`` override the seller's current values
    (a credit note keeps the rate of the document it corrects);
    ``vat_amount`` fixes the VAT of a VAT-inclusive split (receipts).
    """
    with transaction.atomic():
        doc = BillingDocument.objects.select_for_update().get(pk=doc.pk)
        if doc.status != DocStatus.DRAFT:
            raise BillingError(_("%(doc)s is already issued.") % {"doc": doc})
        seller = SellerSettings.load()
        missing = seller.missing_for_issue()
        if missing:
            raise BillingError(
                _("Complete the seller settings first: %(fields)s")
                % {"fields": ", ".join(str(m) for m in missing)}
            )
        _snapshot_seller(doc, seller)
        _snapshot_buyer(doc)
        doc.seller_vat_registered = (
            seller.vat_registered if vat_registered is None else vat_registered
        )
        rate = seller.vat_rate if vat_rate is None else vat_rate
        doc.vat_rate = rate if doc.seller_vat_registered else ZERO
        _compute_totals(doc)
        if vat_amount is not None and doc.seller_vat_registered:
            doc.vat_amount = q(vat_amount)
            doc.total = doc.subtotal + doc.vat_amount
            doc.amount_in_words = baht_text(doc.total)
        if doc.total <= 0:
            raise BillingError(_("The total must be greater than zero."))
        if doc.doc_type == DocType.INVOICE and doc.due_date is None:
            doc.due_date = doc.issue_date + datetime.timedelta(
                days=seller.payment_terms_days
            )
        doc.number = next_number(doc.doc_type, doc.issue_date)
        # Numbers are chronological within a series: refuse to issue a
        # document dated before the latest one of its type (checked while the
        # sequence row lock is held, so two issuers cannot interleave).
        latest = (
            BillingDocument.objects.filter(
                doc_type=doc.doc_type, status=DocStatus.ISSUED
            )
            .exclude(pk=doc.pk)
            .aggregate(latest=Max("issue_date"))["latest"]
        )
        if latest and doc.issue_date < latest:
            raise BillingError(
                _(
                    "Issue date %(date)s is before the latest issued %(type)s "
                    "(%(latest)s); numbers must follow the date order."
                )
                % {
                    "date": doc.issue_date,
                    "type": doc.get_doc_type_display(),
                    "latest": latest,
                }
            )
        doc.status = DocStatus.ISSUED
        doc.issued_at = timezone.now()
        doc.issued_by = by
        doc.save()
    if doc.doc_type == DocType.INVOICE:
        integration.notify("invoice_issued", doc)
    return doc


def delete_draft(doc):
    if doc.is_issued:
        raise BillingError(
            _("Issued documents cannot be deleted; issue a credit note.")
        )
    doc.delete()


# ---------------------------------------------------------------- payments


def _refresh_invoice_status(invoice):
    outstanding = invoice.total - invoice.settled_amount - invoice.credited_amount
    if outstanding <= 0:
        invoice.payment_status = (
            PaymentStatus.PAID if invoice.settled_amount > 0 else PaymentStatus.CREDITED
        )
        invoice.overdue_since = None
    elif invoice.settled_amount > 0:
        invoice.payment_status = PaymentStatus.PARTIAL
    else:
        invoice.payment_status = PaymentStatus.UNPAID
    invoice.save(
        update_fields=[
            "payment_status",
            "overdue_since",
            "settled_amount",
            "credited_amount",
            "updated_at",
        ]
    )


def record_payment(
    invoice,
    *,
    payment_date,
    amount_received,
    wht_amount=0,
    wht_certificate_no="",
    reference="",
    bank_account=None,
    slip=None,
    note="",
    by="",
):
    """
    Record a bank transfer against ``invoice`` and issue the tax
    invoice/receipt for it (for services the tax point is receipt of payment,
    Revenue Code section 78/1). ``wht_amount`` is the tax the customer
    withheld; it settles the invoice like cash. Returns the Payment.
    """
    amount_received = q(amount_received)
    wht_amount = q(wht_amount or 0)
    if amount_received < 0 or wht_amount < 0:
        raise BillingError(_("Amounts cannot be negative."))
    gross = amount_received + wht_amount
    if gross <= 0:
        raise BillingError(_("The payment amount must be greater than zero."))
    if wht_amount and not wht_certificate_no:
        raise BillingError(
            _(
                "Enter the withholding tax certificate (50 ทวิ) number for the amount withheld."
            )
        )

    with transaction.atomic():
        invoice = BillingDocument.objects.select_for_update().get(pk=invoice.pk)
        if invoice.doc_type != DocType.INVOICE or not invoice.is_issued:
            raise BillingError(_("Payments are recorded against issued invoices only."))
        outstanding = invoice.outstanding
        if gross > outstanding:
            raise BillingError(
                _("%(gross)s is more than the %(outstanding)s outstanding.")
                % {"gross": gross, "outstanding": outstanding}
            )

        payment = Payment(
            invoice=invoice,
            payment_date=payment_date,
            amount_received=amount_received,
            wht_amount=wht_amount,
            wht_certificate_no=wht_certificate_no,
            reference=reference,
            bank_account=bank_account,
            note=note,
            recorded_by=by,
        )
        if slip is not None:
            payment.slip = slip
        payment.save()

        receipts = BillingDocument.objects.filter(
            related_document=invoice, doc_type=DocType.RECEIPT, status=DocStatus.ISSUED
        )
        credits = BillingDocument.objects.filter(
            related_document=invoice,
            doc_type=DocType.CREDIT_NOTE,
            status=DocStatus.ISSUED,
        )
        if gross == outstanding and not receipts.exists() and not credits.exists():
            # Paid in one go: the receipt repeats the invoice lines.
            lines = [
                {
                    "description": line.description,
                    "quantity": line.quantity,
                    "unit": line.unit,
                    "unit_price": line.unit_price,
                    "amount": line.amount,
                }
                for line in invoice.lines.all()
            ]
            base, vat = invoice.subtotal, invoice.vat_amount
        else:
            if gross == outstanding:
                # Last instalment: whatever base/VAT is left, so the
                # receipts add up to the invoice exactly.
                done = receipts.aggregate(s=Sum("subtotal"), v=Sum("vat_amount"))
                cn = credits.aggregate(s=Sum("subtotal"), v=Sum("vat_amount"))
                base = invoice.subtotal - (done["s"] or ZERO) - (cn["s"] or ZERO)
                vat = invoice.vat_amount - (done["v"] or ZERO) - (cn["v"] or ZERO)
                label = _("Final payment for invoice %(number)s")
            else:
                base, vat = split_vat_inclusive(gross, invoice.vat_rate)
                label = _("Partial payment for invoice %(number)s")
            lines = [
                {
                    "description": label % {"number": invoice.number},
                    "quantity": 1,
                    "unit_price": base,
                }
            ]

        receipt = create_draft(
            invoice.tenant,
            DocType.RECEIPT,
            lines,
            issue_date=payment_date,
            period_start=invoice.period_start,
            period_end=invoice.period_end,
            subscription=invoice.subscription,
            related_document=invoice,
            by=by,
            wht_amount=wht_amount,
            wht_certificate_no=wht_certificate_no,
            net_received=amount_received,
            notes=(_("Reference: %(ref)s") % {"ref": reference}) if reference else "",
        )
        # The VAT is the split that sums to the cash received; recomputing
        # it from the base could differ by a satang.
        receipt = issue(
            receipt,
            by=by,
            vat_rate=invoice.vat_rate,
            vat_registered=invoice.seller_vat_registered,
            vat_amount=vat,
        )
        if receipt.subtotal != base:  # pragma: no cover - lines are built from base
            raise BillingError("Receipt base does not match the payment split.")

        Payment.objects.filter(pk=payment.pk).update(receipt=receipt)
        payment.receipt = receipt
        invoice.settled_amount = invoice.settled_amount + gross
        _refresh_invoice_status(invoice)
    integration.notify("payment_confirmed", payment)
    return payment


# ------------------------------------------------------------- credit notes


def creditable_amount(original):
    """VAT-exclusive value of ``original`` not yet reduced by credit notes."""
    credited = (
        BillingDocument.objects.filter(
            related_document=original,
            doc_type=DocType.CREDIT_NOTE,
            status=DocStatus.ISSUED,
        ).aggregate(s=Sum("subtotal"))["s"]
        or ZERO
    )
    return original.subtotal - credited


def create_credit_note(
    original, *, amount, reason, description="", issue_date=None, by=""
):
    """
    Issue a credit note reducing ``original`` (an issued invoice or tax
    invoice/receipt) by ``amount`` (VAT-exclusive). VAT is computed at the
    original document's rate.

    * Against an invoice it reduces what is still owed; it can never exceed
      the unpaid part (the paid part has a tax invoice -- credit that).
    * Against a tax invoice/receipt it documents a reduction of a paid
      amount (a refund or a credit the operator settles separately); the
      invoice's payment status does not change.
    """
    amount = q(amount)
    if amount <= 0:
        raise BillingError(_("The credit amount must be greater than zero."))
    if not reason:
        raise BillingError(_("A credit note needs a reason."))
    with transaction.atomic():
        original = BillingDocument.objects.select_for_update().get(pk=original.pk)
        if not original.is_issued or original.doc_type == DocType.CREDIT_NOTE:
            raise BillingError(
                _("Only an issued invoice or tax invoice can be credited.")
            )
        remaining = creditable_amount(original)
        if amount > remaining:
            raise BillingError(
                _(
                    "%(amount)s is more than the %(remaining)s that can still be credited."
                )
                % {"amount": amount, "remaining": remaining}
            )
        vat = (
            vat_on(amount, original.vat_rate)
            if original.seller_vat_registered
            else ZERO
        )
        if original.doc_type == DocType.INVOICE and amount + vat > original.outstanding:
            raise BillingError(
                _(
                    "The credit (%(total)s incl. VAT) is more than the unpaid %(outstanding)s. "
                    "Credit the tax invoice for the paid part instead."
                )
                % {"total": amount + vat, "outstanding": original.outstanding}
            )
        note = create_draft(
            original.tenant,
            DocType.CREDIT_NOTE,
            [
                {
                    "description": description
                    or _("Reduction of %(number)s") % {"number": original.number},
                    "quantity": 1,
                    "unit_price": amount,
                }
            ],
            issue_date=issue_date or _today(),
            related_document=original,
            subscription=original.subscription,
            period_start=original.period_start,
            period_end=original.period_end,
            by=by,
            cn_reason=reason,
            cn_original_value=remaining,
            cn_correct_value=remaining - amount,
        )
        note = issue(
            note,
            by=by,
            vat_rate=original.vat_rate,
            vat_registered=original.seller_vat_registered,
        )
        if original.doc_type == DocType.INVOICE:
            original.credited_amount = original.credited_amount + note.total
            _refresh_invoice_status(original)
    return note


# --------------------------------------------------------------- generation


def count_active_employees(tenant):
    """Active employees inside the tenant's own schema."""
    from horilla.tenancy import use_schema

    with use_schema(tenant.schema_name):
        from employee.models import Employee

        # _base_manager: no company scoping from a request/context variable.
        return Employee._base_manager.filter(is_active=True).count()


def period_bounds(subscription, period_start):
    """
    (start, end, next_start) of the period starting at ``period_start``.

    Periods keep the day of month of ``start_date`` (a subscription started
    on the 31st renews on the last day of shorter months, then on the 31st
    again). A ``next_invoice_date`` moved off that grid by hand simply runs
    one full month/year from where it is. No proration.
    """
    step = 12 if subscription.cycle == BillingCycle.YEARLY else 1
    anchor_day = subscription.start_date.day
    last_day = calendar.monthrange(period_start.year, period_start.month)[1]
    if period_start.day == min(anchor_day, last_day):
        next_start = period_start + relativedelta(months=step, day=anchor_day)
    else:
        next_start = period_start + relativedelta(months=step)
    return period_start, next_start - datetime.timedelta(days=1), next_start


def _period_lines(subscription, price, start, end, employees):
    cycle = subscription.get_cycle_display()
    name = price.description or _("OrvaHR %(plan)s subscription") % {
        "plan": integration.plan_name(subscription.plan_code)
    }
    period = f"{start:%d/%m/%Y} - {end:%d/%m/%Y}"
    lines = []
    if price.base_price > 0 or not price.per_employee_price:
        lines.append(
            {
                "description": f"{name} ({cycle}) {period}",
                "quantity": 1,
                "unit": _("period"),
                "unit_price": price.base_price,
            }
        )
    if price.per_employee_price:
        lines.append(
            {
                "description": _("Active employees, %(period)s") % {"period": period},
                "quantity": employees,
                "unit": _("employee"),
                "unit_price": price.per_employee_price,
            }
        )
    return lines


def generate_invoices(
    on_date=None, *, issue_now=True, by="billing_generate", dry_run=False
):
    """
    Invoice every active subscription whose next period has started by
    ``on_date`` (billing in advance). Idempotent: one invoice per
    subscription period (unique constraint), and ``next_invoice_date`` only
    moves forward. Returns a list of (subscription, period_start, outcome).
    """
    on_date = on_date or _today()
    results = []
    subs = Subscription.objects.filter(
        status=SubscriptionStatus.ACTIVE, next_invoice_date__lte=on_date
    ).select_related("tenant")
    for sub in subs:
        while True:
            outcome = _generate_one(sub.pk, on_date, issue_now, by, dry_run)
            if outcome is None:
                break
            results.append(outcome)
            if dry_run or outcome[2].startswith("error"):
                break
    return results


def _generate_one(sub_pk, on_date, issue_now, by, dry_run):
    try:
        with transaction.atomic():
            sub = (
                Subscription.objects.select_for_update()
                .select_related("tenant")
                .get(pk=sub_pk)
            )
            if (
                sub.status != SubscriptionStatus.ACTIVE
                or sub.next_invoice_date > on_date
            ):
                return None
            start, end, next_start = period_bounds(sub, sub.next_invoice_date)
            if BillingDocument.objects.filter(
                subscription=sub, doc_type=DocType.INVOICE, period_start=start
            ).exists():
                if not dry_run:
                    sub.next_invoice_date = next_start
                    sub.save(update_fields=["next_invoice_date", "updated_at"])
                return (sub, start, "exists")
            price = PlanPrice.current(sub.plan_code, sub.cycle, start)
            if price is None:
                return (
                    sub,
                    start,
                    f"error: no price for {sub.plan_code}/{sub.cycle} on {start}",
                )
            employees = (
                count_active_employees(sub.tenant) if price.per_employee_price else 0
            )
            lines = _period_lines(sub, price, start, end, employees)
            if dry_run:
                total = sum(
                    (line_amount(l["quantity"], l["unit_price"]) for l in lines), ZERO
                )
                return (sub, start, f"would invoice {total} excl. VAT")
            invoice = create_invoice(
                sub.tenant,
                lines,
                issue_date=on_date,
                period_start=start,
                period_end=end,
                subscription=sub,
                by=by,
            )
            if issue_now:
                invoice = issue(invoice, by=by)
            sub.next_invoice_date = next_start
            sub.save(update_fields=["next_invoice_date", "updated_at"])
            return (sub, start, f"created {invoice}")
    except IntegrityError:
        # A concurrent run created this period's invoice first.
        return (Subscription.objects.get(pk=sub_pk), None, "exists")
    except BillingError as exc:
        return (Subscription.objects.get(pk=sub_pk), None, f"error: {exc}")


# ------------------------------------------------------------------ overdue


def _setting(name, default):
    """Django setting, else environment variable, else ``default``."""
    if hasattr(settings, name):
        return getattr(settings, name)
    return os.environ.get(name, default)


def auto_suspend_enabled():
    value = _setting("ORVAHR_BILLING_AUTO_SUSPEND", False)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def default_grace_days():
    return int(_setting("ORVAHR_BILLING_SUSPEND_GRACE_DAYS", 14))


def process_overdue(
    on_date=None, *, grace_days=None, dry_run=False, allow_suspend=None
):
    """
    Mark unpaid issued invoices past their due date as overdue, and list the
    tenants with an invoice overdue for more than ``grace_days``.

    Suspension happens only when ``allow_suspend`` (default: the
    ORVAHR_BILLING_AUTO_SUSPEND setting, False unless set) is true, not in
    ``dry_run``, and the control plane provides ``suspend_tenant``.

    Returns {"marked": [invoice], "suspend": [(tenant, invoice, outcome)]}.
    """
    on_date = on_date or _today()
    grace_days = default_grace_days() if grace_days is None else grace_days
    if allow_suspend is None:
        allow_suspend = auto_suspend_enabled()

    unpaid = BillingDocument.objects.filter(
        doc_type=DocType.INVOICE,
        status=DocStatus.ISSUED,
        payment_status__in=[PaymentStatus.UNPAID, PaymentStatus.PARTIAL],
        due_date__lt=on_date,
    ).select_related("tenant")

    marked = []
    for invoice in unpaid.filter(overdue_since__isnull=True):
        marked.append(invoice)
        if not dry_run:
            BillingDocument.objects.filter(pk=invoice.pk).update(
                overdue_since=invoice.due_date + datetime.timedelta(days=1)
            )

    cutoff = on_date - datetime.timedelta(days=grace_days)
    worst = {}
    for invoice in unpaid.filter(due_date__lt=cutoff).order_by("due_date"):
        worst.setdefault(invoice.tenant_id, invoice)

    suspend_func = integration.get_suspend_tenant() if allow_suspend else None
    suspend = []
    for invoice in worst.values():
        tenant = invoice.tenant
        if getattr(tenant, "is_suspended", False):
            outcome = "already suspended"
        elif dry_run:
            outcome = "would suspend" if allow_suspend else "suspension disabled"
        elif not allow_suspend:
            outcome = "suspension disabled"
        elif suspend_func is None:
            outcome = "suspend_tenant unavailable"
            logger.warning(
                "Billing wants to suspend %s but orvahr_saas provides no suspend_tenant()",
                tenant,
            )
        else:
            reason = "Billing: invoice %s unpaid since %s (due %s, grace %s days)" % (
                invoice.number,
                invoice.due_date + datetime.timedelta(days=1),
                invoice.due_date,
                grace_days,
            )
            try:
                suspend_func(tenant, reason)
                outcome = "suspended"
            except Exception:  # noqa: BLE001 - one tenant must not stop the run
                logger.exception("suspend_tenant failed for %s", tenant)
                outcome = "error"
        suspend.append((tenant, invoice, outcome))
    return {"marked": marked, "suspend": suspend}


# -------------------------------------------------------------------- email


def _email_connection():
    backend = getattr(settings, "ORVAHR_BILLING_EMAIL_BACKEND", None)
    if not backend:
        backend = settings.EMAIL_BACKEND
        # Horilla's backend reads the mail server from tenant tables, which
        # the public schema does not have.
        if backend == "base.backends.ConfiguredEmailBackend":
            backend = "django.core.mail.backends.smtp.EmailBackend"
    from django.core.mail import get_connection

    return get_connection(backend)


def send_document(doc, *, to=None, lang="th", by=""):
    """E-mail the PDF of an issued document to ``to`` or the buyer's e-mail."""
    from django.core.mail import EmailMessage

    from orvahr_billing.pdf import render_pdf

    if not doc.is_issued:
        raise BillingError(_("Only issued documents can be sent."))
    recipient = to or doc.buyer_email
    if not recipient:
        raise BillingError(_("No e-mail address for this customer."))
    pdf = render_pdf(doc, lang=lang)
    seller = (
        doc.seller_name_en
        if lang == "en" and doc.seller_name_en
        else doc.seller_name_th
    )
    subject = (
        f"{doc.title_en if lang == 'en' else doc.title_th} {doc.number} - {seller}"
    )
    body = _("Please find attached %(title)s %(number)s, total %(total)s THB.") % {
        "title": doc.title_en,
        "number": doc.number,
        "total": f"{doc.total:,.2f}",
    }

    # base.backends patches EmailMessage.__init__ to read the tenant's mail
    # configuration; on the public schema use the original initialiser.
    try:
        from base import backends as horilla_backends

        original_init = getattr(horilla_backends, "message_init", None)
    except Exception:  # noqa: BLE001
        original_init = None
    message = EmailMessage.__new__(EmailMessage)
    (original_init or EmailMessage.__init__)(
        message,
        subject=subject,
        body=body,
        from_email=getattr(settings, "ORVAHR_BILLING_FROM_EMAIL", None)
        or doc.seller_email
        or settings.DEFAULT_FROM_EMAIL,
        to=[recipient],
        connection=_email_connection(),
    )
    message.attach(f"{doc.number}.pdf", pdf, "application/pdf")
    message.send()
    BillingDocument.objects.filter(pk=doc.pk).update(emailed_at=timezone.now())
    return recipient
