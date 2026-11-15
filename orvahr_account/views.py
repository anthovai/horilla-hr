"""
Subscription & billing pages for a tenant's administrators (SaaS mode only;
the URLs are mounted by ``OrvahrAccountConfig.ready()`` only then).

Access: superusers, or users holding ``orvahr_account.manage_subscription``
(assignable to a Horilla role / group). Everything shown is read from the
public control-plane tables filtered by the tenant the connection is bound to
(``orvahr_account.billing.bound_tenant``) -- never by an id from the URL.

URLs and identifiers
--------------------
Billing documents are addressed by their public number (``INV2569-000012``),
not their database id. The number is printed on the document the tenant
already holds, so the URL discloses nothing new; the primary key, by
contrast, counts drafts and every other customer's documents and would leak
the operator's volume. Lookups are always ``(tenant, number, issued)``, so a
guessed number of another customer -- or of a draft -- answers 404.
Payment notices and plan requests are addressed by a random UUID token.
"""

from __future__ import annotations

import logging
from functools import wraps

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from horilla.decorators import login_required
from orvahr_account import billing
from orvahr_account.forms import (
    PaymentNoticeForm,
    PlanChangeForm,
    billing_profile_form_class,
)
from orvahr_account.models import MANAGE_PERMISSION, PaymentNotice, PlanChangeRequest
from orvahr_account.services import invalidate_pending_counts

logger = logging.getLogger(__name__)


def can_manage(user):
    return bool(
        user.is_authenticated
        and user.is_active
        and (user.is_superuser or user.has_perm(MANAGE_PERMISSION))
    )


def subscription_admin_required(view):
    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not can_manage(request.user):
            raise PermissionDenied
        tenant = billing.bound_tenant()  # 404 off a real tenant host
        return view(request, tenant, *args, **kwargs)

    return wrapper


def _private(response):
    response["Cache-Control"] = "no-store, private"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _module_labels(plan):
    from orvahr_saas.plans import MODULE_CATALOG

    if plan is None:
        return [str(label) for label in MODULE_CATALOG.values()]
    return [str(MODULE_CATALOG[m]) for m in plan.modules or [] if m in MODULE_CATALOG]


def _pending_plan_request():
    return PlanChangeRequest.objects.filter(
        status=PlanChangeRequest.STATUS_PENDING
    ).first()


# ---------------------------------------------------------------- overview


@subscription_admin_required
@require_http_methods(["GET", "POST"])
def overview(request, tenant):
    from orvahr_saas.limits import active_employee_count, employee_limit
    from orvahr_saas.plans import tenant_plan

    profile = billing.billing_profile(tenant)
    form_class = billing_profile_form_class()
    if request.method == "POST":
        form = form_class(request.POST, instance=profile)
        form.instance.tenant_id = tenant.pk  # always this tenant, never posted
        if form.is_valid():
            with transaction.atomic():
                saved = form.save(commit=False)
                saved.tenant_id = tenant.pk
                saved.save()
            messages.success(request, _("Billing details saved."))
            return redirect("orvahr_account:overview")
    else:
        form = form_class(instance=profile)

    plan = tenant_plan(tenant)
    subscription = billing.subscription(tenant)
    context = {
        "tenant": tenant,
        "plan": plan,
        "modules": _module_labels(plan),
        "status": billing.account_status(tenant),
        "active_employees": active_employee_count(),
        "employee_limit": employee_limit(),
        "subscription": subscription,
        "profile": profile,
        "form": form,
        "open_invoices": list(billing.open_invoices(tenant)),
        "pending_plan_request": _pending_plan_request(),
        "pending_notices": PaymentNotice.objects.filter(
            status=PaymentNotice.STATUS_PENDING
        ).count(),
        "edit_profile": request.method == "POST" or request.GET.get("edit") == "1",
    }
    return _private(render(request, "orvahr_account/overview.html", context))


# --------------------------------------------------------------- documents


@subscription_admin_required
@require_http_methods(["GET"])
def documents(request, tenant):
    docs = list(billing.issued_documents(tenant).select_related("related_document"))
    notices = list(PaymentNotice.objects.all()[:50])
    pending_numbers = {n.invoice_number for n in notices if n.is_pending}
    payable_numbers = {
        doc.number
        for doc in docs
        if doc.doc_type == "invoice" and doc.payment_status in ("unpaid", "partial")
    } - pending_numbers
    return _private(
        render(
            request,
            "orvahr_account/documents.html",
            {
                "documents": docs,
                "notices": notices,
                "pending_numbers": pending_numbers,
                "payable_numbers": payable_numbers,
            },
        )
    )


@subscription_admin_required
@require_http_methods(["GET"])
def document_pdf(request, tenant, number):
    from orvahr_billing.pdf import LANGUAGES, render_pdf

    doc = billing.get_issued_document(tenant, number)
    lang = request.GET.get("lang", "th")
    if lang not in LANGUAGES:
        lang = "th"
    try:
        pdf = render_pdf(doc, lang)
    except Exception:  # noqa: BLE001 - logged; the tenant sees a 503, not a trace
        logger.exception("PDF of %s failed", doc.number)
        return HttpResponse(_("The PDF could not be produced."), status=503)
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{doc.number}-{lang}.pdf"'
    return _private(response)


# ---------------------------------------------------------- payment notices


@subscription_admin_required
@require_http_methods(["GET", "POST"])
def payment_notify(request, tenant):
    invoices = list(billing.open_invoices(tenant))
    if request.method == "POST":
        form = PaymentNoticeForm(request.POST, request.FILES, invoices=invoices)
        if form.is_valid():
            data = form.cleaned_data
            PaymentNotice.objects.create(
                invoice_number=data["invoice_document"].number,
                amount=data["amount"],
                transfer_date=data["transfer_date"],
                reference=data.get("reference", ""),
                wht_amount=data["wht_amount"],
                wht_certificate_no=data.get("wht_certificate_no", ""),
                slip=data["slip"],
                slip_content_type=form.slip_content_type,
                note=data.get("note", ""),
                submitted_by=request.user,
            )
            invalidate_pending_counts()  # operator nav badge
            messages.success(
                request,
                _("Thank you. We will confirm your payment and issue the receipt."),
            )
            return redirect("orvahr_account:documents")
    else:
        initial = {}
        wanted = request.GET.get("invoice")
        if wanted and any(doc.number == wanted for doc in invoices):
            initial["invoice"] = wanted
            initial["amount"] = next(
                doc.outstanding for doc in invoices if doc.number == wanted
            )
        form = PaymentNoticeForm(initial=initial, invoices=invoices)
    return _private(
        render(
            request,
            "orvahr_account/payment_notify.html",
            {"form": form, "invoices": invoices},
        )
    )


@subscription_admin_required
@require_http_methods(["GET"])
def payment_notice_slip(request, tenant, token):
    # The table is in this tenant's schema: another tenant's token is simply
    # not there, and the file is resolved inside this schema's folder.
    notice = PaymentNotice.objects.filter(token=token).first()
    if notice is None or not notice.slip:
        raise Http404
    try:
        handle = notice.slip.open("rb")
    except OSError as exc:
        raise Http404 from exc
    name = notice.slip.name.rsplit("/", 1)[-1]
    response = FileResponse(
        handle,
        content_type=notice.slip_content_type or "application/octet-stream",
    )
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    return _private(response)


# ------------------------------------------------------------ plan change


@subscription_admin_required
@require_http_methods(["GET", "POST"])
def plan_change(request, tenant):
    from orvahr_saas.plans import plan_prices, tenant_plan

    current = tenant_plan(tenant)
    plans = list(billing.public_plans())
    choices = [p for p in plans if current is None or p.pk != current.pk]
    pending = _pending_plan_request()
    if request.method == "POST":
        if pending is not None:
            messages.info(request, _("A plan change request is already pending."))
            return redirect("orvahr_account:plan-change")
        form = PlanChangeForm(request.POST, plans=choices)
        if form.is_valid():
            PlanChangeRequest.objects.create(
                current_plan_code=current.code if current else "",
                requested_plan_code=form.cleaned_data["plan"],
                note=form.cleaned_data.get("note", ""),
                requested_by=request.user,
            )
            invalidate_pending_counts()  # operator nav badge
            messages.success(
                request, _("Your request was sent. OrvaHR will contact you shortly.")
            )
            return redirect("orvahr_account:plan-change")
    else:
        form = PlanChangeForm(plans=choices)
    from orvahr_saas.plans import MODULE_CATALOG

    prices = plan_prices(plans)
    rows = [
        {
            "plan": plan,
            "is_current": current is not None and plan.pk == current.pk,
            "price": prices.get(plan.code, ""),
            "modules": [
                str(MODULE_CATALOG[m]) for m in plan.modules or [] if m in MODULE_CATALOG
            ],
        }
        for plan in plans
    ]
    return _private(
        render(
            request,
            "orvahr_account/plan_change.html",
            {
                "rows": rows,
                "form": form,
                "current": current,
                "pending": pending,
                "pending_plan": next(
                    (
                        p
                        for p in plans
                        if pending and p.code == pending.requested_plan_code
                    ),
                    None,
                ),
                "history": PlanChangeRequest.objects.exclude(
                    status=PlanChangeRequest.STATUS_PENDING
                )[:10],
            },
        )
    )


@subscription_admin_required
@require_POST
def plan_change_cancel(request, tenant, token):
    updated = PlanChangeRequest.objects.filter(
        token=token, status=PlanChangeRequest.STATUS_PENDING
    ).update(status=PlanChangeRequest.STATUS_CANCELLED)
    if not updated:
        raise Http404
    invalidate_pending_counts()
    messages.info(request, _("The plan change request was cancelled."))
    return redirect("orvahr_account:plan-change")
