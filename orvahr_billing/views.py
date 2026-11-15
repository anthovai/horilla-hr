"""
Operator UI at /billing/ on the public host. Every view is wrapped in
``auth.operator_required`` (active staff console operators only).

Pages are rendered without a request context on purpose: Horilla's context
processors query tenant tables the public schema does not have. The CSRF
token and messages are passed explicitly instead.
"""

import mimetypes
import os

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse, HttpResponseRedirect
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from orvahr_billing import services
from orvahr_billing.auth import operator_label, operator_required
from orvahr_billing.forms import (
    BankAccountFormSet,
    BillingProfileForm,
    CreditNoteForm,
    DocumentFilterForm,
    EmailForm,
    InvoiceForm,
    LineFormSet,
    PaymentForm,
    PlanPriceForm,
    SellerSettingsForm,
    SubscriptionForm,
)
from orvahr_billing.models import (
    BankAccount,
    BillingDocument,
    BillingProfile,
    DocStatus,
    DocType,
    Payment,
    PaymentStatus,
    PlanPrice,
    SellerSettings,
    Subscription,
)
from orvahr_billing.money import wht_on
from orvahr_billing.pdf import LANGUAGES, render_pdf


def _render(request, template, context=None, status=200):
    from django.contrib.messages import get_messages

    ctx = {
        "csrf_token": get_token(request),
        "operator": getattr(request, "operator", None),
        "messages": list(get_messages(request)),
        "request_path": request.path,
    }
    ctx.update(context or {})
    return HttpResponse(render_to_string(template, ctx), status=status)


def _redirect(name, *args):
    return HttpResponseRedirect(reverse(f"orvahr_billing:{name}", args=args))


def _tenant_model():
    from orvahr_saas.models import Tenant

    return Tenant


# ------------------------------------------------------------- documents


@operator_required
def document_list(request):
    form = DocumentFilterForm(request.GET or None)
    docs = BillingDocument.objects.select_related("tenant").order_by(
        "-issue_date", "-pk"
    )
    if form.is_valid():
        data = form.cleaned_data
        if data["doc_type"]:
            docs = docs.filter(doc_type=data["doc_type"])
        if data["status"]:
            docs = docs.filter(status=data["status"])
        if data["payment_status"]:
            docs = docs.filter(
                doc_type=DocType.INVOICE, payment_status=data["payment_status"]
            )
        if data["overdue"]:
            docs = docs.filter(
                doc_type=DocType.INVOICE,
                status=DocStatus.ISSUED,
                payment_status__in=[PaymentStatus.UNPAID, PaymentStatus.PARTIAL],
                due_date__lt=timezone.localdate(),
            )
        if data["tenant"]:
            docs = docs.filter(
                Q(tenant__name__icontains=data["tenant"])
                | Q(tenant__schema_name__icontains=data["tenant"])
            )
        if data["q"]:
            docs = docs.filter(
                Q(number__icontains=data["q"]) | Q(buyer_name__icontains=data["q"])
            )
        if data["date_from"]:
            docs = docs.filter(issue_date__gte=data["date_from"])
        if data["date_to"]:
            docs = docs.filter(issue_date__lte=data["date_to"])
    page = Paginator(docs, 50).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    return _render(
        request,
        "orvahr_billing/document_list.html",
        {
            "form": form,
            "page": page,
            "query": query.urlencode(),
            "today": timezone.localdate(),
        },
    )


@operator_required
def document_detail(request, pk):
    doc = get_object_or_404(
        BillingDocument.objects.select_related("tenant", "related_document"), pk=pk
    )
    expected_wht = None
    if doc.doc_type == DocType.INVOICE:
        expected_wht = wht_on(doc.subtotal, 3)
    return _render(
        request,
        "orvahr_billing/document_detail.html",
        {
            "doc": doc,
            "lines": doc.lines.all(),
            "payments": (
                doc.payments.select_related("receipt", "bank_account")
                if doc.doc_type == DocType.INVOICE
                else []
            ),
            "derived": doc.derived_documents.order_by("pk"),
            "expected_wht": expected_wht,
            "email_form": EmailForm(initial={"to": doc.buyer_email}),
            "today": timezone.localdate(),
        },
    )


@operator_required
@require_http_methods(["GET", "POST"])
def invoice_create(request):
    form = InvoiceForm(
        request.POST or None, initial={"tenant": request.GET.get("tenant")}
    )
    lines = LineFormSet(request.POST or None, prefix="lines")
    if request.method == "POST" and form.is_valid() and lines.is_valid():
        data = form.cleaned_data
        line_data = [l for l in lines.cleaned_data if l and l.get("description")]
        try:
            doc = services.create_invoice(
                data["tenant"],
                line_data,
                issue_date=data["issue_date"],
                due_date=data["due_date"],
                period_start=data["period_start"],
                period_end=data["period_end"],
                notes=data["notes"],
                by=operator_label(request),
            )
        except services.BillingError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(
                request, _("Draft invoice created. Check it, then issue it.")
            )
            return _redirect("document-detail", doc.pk)
    return _render(
        request,
        "orvahr_billing/invoice_form.html",
        {"form": form, "lines": lines},
    )


@operator_required
@require_POST
def document_issue(request, pk):
    doc = get_object_or_404(BillingDocument, pk=pk)
    try:
        doc = services.issue(doc, by=operator_label(request))
    except services.BillingError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _("%(number)s issued.") % {"number": doc.number})
    return _redirect("document-detail", pk)


@operator_required
@require_POST
def document_delete(request, pk):
    doc = get_object_or_404(BillingDocument, pk=pk)
    try:
        services.delete_draft(doc)
    except services.BillingError as exc:
        messages.error(request, str(exc))
        return _redirect("document-detail", pk)
    messages.success(request, _("Draft deleted."))
    return _redirect("document-list")


@operator_required
def document_pdf(request, pk):
    doc = get_object_or_404(BillingDocument, pk=pk)
    lang = request.GET.get("lang", "th")
    if lang not in LANGUAGES:
        lang = "th"
    data = render_pdf(doc, lang=lang)
    name = doc.number or f"draft-{doc.pk}"
    response = HttpResponse(data, content_type="application/pdf")
    disposition = "attachment" if request.GET.get("download") else "inline"
    response["Content-Disposition"] = f'{disposition}; filename="{name}-{lang}.pdf"'
    return response


@operator_required
@require_POST
def document_email(request, pk):
    doc = get_object_or_404(BillingDocument, pk=pk)
    form = EmailForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Enter a valid e-mail address."))
        return _redirect("document-detail", pk)
    try:
        sent_to = services.send_document(
            doc,
            to=form.cleaned_data["to"] or None,
            lang=form.cleaned_data["lang"],
            by=operator_label(request),
        )
    except services.BillingError as exc:
        messages.error(request, str(exc))
    except Exception as exc:  # noqa: BLE001 - SMTP errors are shown, not 500
        messages.error(request, _("Sending failed: %(error)s") % {"error": exc})
    else:
        messages.success(request, _("Sent to %(to)s.") % {"to": sent_to})
    return _redirect("document-detail", pk)


# --------------------------------------------------------------- payments


@operator_required
@require_http_methods(["GET", "POST"])
def payment_create(request, pk):
    invoice = get_object_or_404(BillingDocument, pk=pk, doc_type=DocType.INVOICE)
    if not invoice.is_issued:
        messages.error(request, _("Issue the invoice before recording a payment."))
        return _redirect("document-detail", pk)
    form = PaymentForm(
        request.POST or None,
        request.FILES or None,
        initial={"amount_received": invoice.outstanding},
    )
    form.fields["bank_account"].queryset = BankAccount.objects.filter(is_active=True)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            payment = services.record_payment(
                invoice,
                payment_date=data["payment_date"],
                amount_received=data["amount_received"],
                wht_amount=data["wht_amount"] or 0,
                wht_certificate_no=data["wht_certificate_no"],
                reference=data["reference"],
                bank_account=data["bank_account"],
                slip=data["slip"],
                note=data["note"],
                by=operator_label(request),
            )
        except services.BillingError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(
                request,
                _("Payment recorded; %(number)s issued.")
                % {"number": payment.receipt.number},
            )
            return _redirect("document-detail", payment.receipt.pk)
    return _render(
        request,
        "orvahr_billing/payment_form.html",
        {"form": form, "invoice": invoice, "expected_wht": wht_on(invoice.subtotal, 3)},
    )


@operator_required
def payment_slip(request, pk):
    payment = get_object_or_404(Payment, pk=pk)
    if not payment.slip:
        raise Http404
    try:
        handle = payment.slip.open("rb")
    except OSError as exc:
        raise Http404 from exc
    name = os.path.basename(payment.slip.name)
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    response = FileResponse(handle, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response


# ----------------------------------------------------------- credit notes


@operator_required
@require_http_methods(["GET", "POST"])
def credit_note_create(request, pk):
    original = get_object_or_404(
        BillingDocument,
        pk=pk,
        status=DocStatus.ISSUED,
        doc_type__in=[DocType.INVOICE, DocType.RECEIPT],
    )
    form = CreditNoteForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            note = services.create_credit_note(
                original,
                amount=data["amount"],
                reason=data["reason"],
                description=data["description"],
                issue_date=data["issue_date"],
                by=operator_label(request),
            )
        except services.BillingError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, _("%(number)s issued.") % {"number": note.number})
            return _redirect("document-detail", note.pk)
    return _render(
        request,
        "orvahr_billing/credit_note_form.html",
        {
            "form": form,
            "original": original,
            "creditable": services.creditable_amount(original),
        },
    )


# --------------------------------------------------------------- settings


@operator_required
@require_http_methods(["GET", "POST"])
def seller_settings(request):
    seller = SellerSettings.load()
    form = SellerSettingsForm(
        request.POST or None, request.FILES or None, instance=seller
    )
    accounts = BankAccountFormSet(
        request.POST or None, queryset=BankAccount.objects.all(), prefix="banks"
    )
    if request.method == "POST" and form.is_valid() and accounts.is_valid():
        form.save()
        accounts.save()
        messages.success(request, _("Seller settings saved."))
        return _redirect("seller-settings")
    return _render(
        request,
        "orvahr_billing/seller_settings.html",
        {"form": form, "accounts": accounts},
    )


@operator_required
def tenant_list(request):
    Tenant = _tenant_model()
    tenants = Tenant.objects.exclude(schema_name="public").order_by("name")
    profiles = {p.tenant_id: p for p in BillingProfile.objects.all()}
    subs = {s.tenant_id: s for s in Subscription.objects.all()}
    rows = [(t, profiles.get(t.pk), subs.get(t.pk)) for t in tenants]
    return _render(request, "orvahr_billing/tenant_list.html", {"rows": rows})


@operator_required
@require_http_methods(["GET", "POST"])
def tenant_billing(request, pk):
    tenant = get_object_or_404(_tenant_model(), pk=pk)
    profile = BillingProfile.objects.filter(tenant=tenant).first() or BillingProfile(
        tenant=tenant,
        legal_name=tenant.name,
        email=getattr(tenant, "contact_email", ""),
    )
    subscription = Subscription.objects.filter(tenant=tenant).first()
    if subscription is None:
        today = timezone.localdate()
        subscription = Subscription(
            tenant=tenant,
            plan_code=getattr(tenant, "plan_code", "") or "",
            start_date=today,
            next_invoice_date=today,
        )
    which = request.POST.get("form") if request.method == "POST" else None
    profile_form = BillingProfileForm(
        request.POST if which == "profile" else None, instance=profile, prefix="profile"
    )
    sub_form = SubscriptionForm(
        request.POST if which == "subscription" else None,
        instance=subscription,
        prefix="sub",
        initial={"plan_code": subscription.plan_code},
    )
    if which == "profile" and profile_form.is_valid():
        profile_form.save()
        messages.success(request, _("Billing profile saved."))
        return _redirect("tenant-billing", pk)
    if which == "subscription" and sub_form.is_valid():
        sub_form.save()
        messages.success(request, _("Subscription saved."))
        return _redirect("tenant-billing", pk)
    docs = BillingDocument.objects.filter(tenant=tenant).order_by("-issue_date", "-pk")[
        :50
    ]
    return _render(
        request,
        "orvahr_billing/tenant_billing.html",
        {
            "tenant": tenant,
            "profile_form": profile_form,
            "sub_form": sub_form,
            "docs": docs,
        },
    )


@operator_required
@require_http_methods(["GET", "POST"])
def price_list(request):
    form = PlanPriceForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("Price saved."))
        return _redirect("price-list")
    return _render(
        request,
        "orvahr_billing/price_list.html",
        {"form": form, "prices": PlanPrice.objects.all()},
    )
