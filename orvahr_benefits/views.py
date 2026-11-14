"""
HR pages and employee self-service for provident fund, final settlements and
deduction orders.

Permissions
-----------
* Plans: ``orvahr_benefits.view/add/change_providentfundplan``.
* Members, contribution report / CSV: ``view_employeeprovidentfund``;
  create/edit and "Sync to payroll": ``add/change_employeeprovidentfund``.
* Settlements: ``view/add/change_finalsettlement``; approve:
  ``approve_finalsettlement``. An employee may view (and download) their own.
* Deduction orders and remittance: ``view/add/change_deductionorder``.
  The reference number is shown masked everywhere, including exports.
* Severance rules: ``view_severancerule`` (edited in the admin).
* "My benefits": any logged-in user, for their own employee record only.

URLs carry database ids only. Nothing here logs personal data.
"""

import csv
import datetime

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from horilla.decorators import login_required, permission_required
from orvahr_benefits.forms import (
    DeductionOrderForm,
    EmployeeProvidentFundForm,
    FinalSettlementForm,
    MonthForm,
    ProvidentFundPlanForm,
    VestingStepFormSet,
)
from orvahr_benefits.models import (
    DeductionOrder,
    EmployeeProvidentFund,
    FinalSettlement,
    ProvidentFundPlan,
    SeveranceRule,
)
from orvahr_benefits.services import orders as order_service
from orvahr_benefits.services import pvd
from orvahr_benefits.services import settlement as settlement_service

APP = "orvahr_benefits"


def _no_store(response):
    response["Cache-Control"] = "no-store, private"
    return response


def _own_employee(request):
    return getattr(request.user, "employee_get", None)


def _default_month():
    today = datetime.date.today()
    first = today.replace(day=1) - datetime.timedelta(days=1)
    return first.year, first.month


def _form_page(request, form, title, back, formset=None, extra=None):
    context = {"form": form, "formset": formset, "title": title, "back": back}
    context.update(extra or {})
    return render(request, "orvahr_benefits/form.html", context)


# ---------------------------------------------------------------- home


@login_required
def home(request):
    perms = request.user
    links = []
    if perms.has_perm(f"{APP}.view_providentfundplan"):
        links.append(("orvahr_benefits:pvd-plans", _("Provident fund plans")))
    if perms.has_perm(f"{APP}.view_employeeprovidentfund"):
        links.append(("orvahr_benefits:pvd-members", _("Provident fund members")))
        links.append(("orvahr_benefits:pvd-report", _("Monthly contribution report")))
    if perms.has_perm(f"{APP}.view_finalsettlement"):
        links.append(("orvahr_benefits:settlements", _("Final settlements")))
    if perms.has_perm(f"{APP}.view_severancerule"):
        links.append(("orvahr_benefits:severance-rules", _("Severance rules")))
    if perms.has_perm(f"{APP}.view_deductionorder"):
        links.append(("orvahr_benefits:orders", _("Student loan & garnishment orders")))
        links.append(("orvahr_benefits:remittance", _("Monthly remittance report")))
    links.append(("orvahr_benefits:my-benefits", _("My provident fund & settlements")))
    return render(request, "orvahr_benefits/home.html", {"links": links})


# ---------------------------------------------------------------- PVD plans


@login_required
@permission_required(f"{APP}.view_providentfundplan")
def plan_list(request):
    plans = ProvidentFundPlan.objects.all().prefetch_related("vesting_steps")
    return render(request, "orvahr_benefits/pvd_plans.html", {"plans": plans})


def _plan_edit(request, plan):
    form = ProvidentFundPlanForm(request.POST or None, instance=plan)
    formset = VestingStepFormSet(request.POST or None, instance=plan, prefix="steps")
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        with transaction.atomic():
            saved = form.save()
            formset.instance = saved
            formset.save()
        messages.success(request, _("Plan saved."))
        return redirect("orvahr_benefits:pvd-plans")
    return _form_page(
        request,
        form,
        _("Provident fund plan"),
        "orvahr_benefits:pvd-plans",
        formset=formset,
        extra={"formset_title": _("Vesting schedule")},
    )


@login_required
@permission_required(f"{APP}.add_providentfundplan")
def plan_create(request):
    return _plan_edit(request, None)


@login_required
@permission_required(f"{APP}.change_providentfundplan")
def plan_update(request, pk):
    return _plan_edit(request, get_object_or_404(ProvidentFundPlan.objects.all(), pk=pk))


# ---------------------------------------------------------------- PVD members


@login_required
@permission_required(f"{APP}.view_employeeprovidentfund")
def member_list(request):
    members = EmployeeProvidentFund.objects.all().select_related("employee", "plan")
    plan_id = request.GET.get("plan")
    if plan_id and plan_id.isdigit():
        members = members.filter(plan_id=int(plan_id))
    return render(
        request,
        "orvahr_benefits/pvd_members.html",
        {"members": members, "plans": ProvidentFundPlan.objects.all(), "plan_id": plan_id},
    )


def _member_edit(request, member):
    initial = {}
    if member is None:
        plan = ProvidentFundPlan.objects.filter(is_active=True).first()
        if plan:
            initial = {
                "plan": plan,
                "employee_rate": plan.default_employee_rate,
                "employer_rate": plan.default_employer_rate,
                "joined_on": datetime.date.today(),
            }
    form = EmployeeProvidentFundForm(request.POST or None, instance=member, initial=initial)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(
            request,
            _("Membership saved. Run \"Sync to payroll\" so the deduction follows it."),
        )
        return redirect("orvahr_benefits:pvd-members")
    return _form_page(request, form, _("Provident fund member"), "orvahr_benefits:pvd-members")


@login_required
@permission_required(f"{APP}.add_employeeprovidentfund")
def member_create(request):
    return _member_edit(request, None)


@login_required
@permission_required(f"{APP}.change_employeeprovidentfund")
def member_update(request, pk):
    return _member_edit(
        request, get_object_or_404(EmployeeProvidentFund.objects.all(), pk=pk)
    )


@login_required
@permission_required(f"{APP}.change_employeeprovidentfund")
@require_POST
def pvd_sync(request):
    summary = pvd.sync_pvd_deductions()
    messages.success(
        request,
        _(
            "Payroll deductions synced: %(created)s created, %(updated)s updated, "
            "%(changes)s membership changes."
        )
        % {
            "created": summary["created"],
            "updated": summary["updated"],
            "changes": summary["membership_changes"],
        },
    )
    return redirect("orvahr_benefits:pvd-members")


@login_required
@permission_required(f"{APP}.change_employeeprovidentfund")
@require_POST
def pvd_tax_claims(request):
    try:
        year = int(request.POST.get("year", ""))
    except ValueError:
        year = datetime.date.today().year
    if not 2000 <= year <= 2200:
        year = datetime.date.today().year
    summary = pvd.upsert_pvd_tax_claims(year)
    messages.success(
        request,
        _("Provident fund tax claims for %(year)s: %(created)s created, %(updated)s updated.")
        % {"year": year, "created": summary["created"], "updated": summary["updated"]},
    )
    return redirect("orvahr_benefits:pvd-members")


def _report_rows(request):
    form = MonthForm(request.GET)
    year, month = form.year_month(_default_month())
    plan = None
    plan_id = request.GET.get("plan")
    if plan_id and plan_id.isdigit():
        plan = ProvidentFundPlan.objects.filter(pk=int(plan_id)).first()
    # Only members visible under the selected company.
    visible = set(EmployeeProvidentFund.objects.values_list("pk", flat=True))
    rows = [
        r for r in pvd.monthly_contributions(year, month, plan=plan) if r["member"].pk in visible
    ]
    return year, month, plan, rows


@login_required
@permission_required(f"{APP}.view_employeeprovidentfund")
def pvd_report(request):
    year, month, plan, rows = _report_rows(request)
    return render(
        request,
        "orvahr_benefits/pvd_report.html",
        {
            "rows": rows,
            "totals": pvd.totals(rows),
            "month_value": f"{year:04d}-{month:02d}",
            "plans": ProvidentFundPlan.objects.all(),
            "plan": plan,
        },
    )


@login_required
@permission_required(f"{APP}.view_employeeprovidentfund")
def pvd_report_csv(request):
    """
    Generic CSV for the fund manager. NOT any specific fund manager's upload
    format: map the columns to theirs. No national / tax IDs.
    """
    year, month, plan, rows = _report_rows(request)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="pvd-contributions-generic-{year:04d}-{month:02d}.csv"'
    )
    response.write("﻿")
    writer = csv.writer(response)
    writer.writerow(
        [
            "format",
            "plan",
            "fund_manager",
            "badge_id",
            "employee_name",
            "period_start",
            "period_end",
            "contribution_base",
            "employee_rate_pct",
            "employer_rate_pct",
            "employee_contribution",
            "employer_contribution",
            "total",
            "matches_expected",
        ]
    )
    for r in rows:
        emp_amount = r["deducted_employee"]
        er_amount = r["deducted_employer"]
        writer.writerow(
            [
                "orvahr-generic-v1",
                r["plan"].name,
                r["plan"].fund_manager_name,
                r["employee"].badge_id or "",
                r["employee"].get_full_name(),
                r["period_start"].isoformat(),
                r["period_end"].isoformat(),
                r["base"],
                r["employee_rate"],
                r["employer_rate"],
                "" if emp_amount is None else emp_amount,
                "" if er_amount is None else er_amount,
                "" if emp_amount is None else emp_amount + er_amount,
                "no" if r["mismatch"] else "yes",
            ]
        )
    return _no_store(response)


# ---------------------------------------------------------------- severance rules


@login_required
@permission_required(f"{APP}.view_severancerule")
def severance_rules(request):
    return render(
        request,
        "orvahr_benefits/severance_rules.html",
        {"rules": SeveranceRule.objects.all()},
    )


# ---------------------------------------------------------------- settlements


def _settlement_for(request, pk):
    """The settlement if the user is HR (company-scoped) or its own employee."""
    if request.user.has_perm(f"{APP}.view_finalsettlement"):
        return get_object_or_404(
            FinalSettlement.objects.all().select_related("employee"), pk=pk
        )
    own = _own_employee(request)
    # An employee sees their own settlement once HR has approved it.
    settlement = (
        FinalSettlement.objects.entire()
        .filter(pk=pk)
        .exclude(status=FinalSettlement.STATUS_DRAFT)
        .first()
    )
    if settlement is None or own is None or settlement.employee_id != own.pk:
        raise Http404
    return settlement


@login_required
@permission_required(f"{APP}.view_finalsettlement")
def settlement_list(request):
    settlements = FinalSettlement.objects.all().select_related("employee")
    status = request.GET.get("status")
    if status in dict(FinalSettlement.STATUS_CHOICES):
        settlements = settlements.filter(status=status)
    return render(
        request,
        "orvahr_benefits/settlements.html",
        {
            "settlements": settlements,
            "statuses": FinalSettlement.STATUS_CHOICES,
            "status": status,
        },
    )


@login_required
@permission_required(f"{APP}.add_finalsettlement")
def settlement_create(request):
    form = FinalSettlementForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        settlement = settlement_service.compute(form.save(commit=False))
        messages.success(request, _("Settlement drafted. Check every figure."))
        return redirect("orvahr_benefits:settlement-detail", pk=settlement.pk)
    return _form_page(request, form, _("New final settlement"), "orvahr_benefits:settlements")


@login_required
@permission_required(f"{APP}.change_finalsettlement")
def settlement_update(request, pk):
    settlement = get_object_or_404(FinalSettlement.objects.all(), pk=pk)
    if not settlement.is_draft:
        messages.error(request, _("Only a draft settlement can be changed."))
        return redirect("orvahr_benefits:settlement-detail", pk=pk)
    form = FinalSettlementForm(request.POST or None, instance=settlement)
    if request.method == "POST" and form.is_valid():
        settlement_service.compute(form.save(commit=False))
        messages.success(request, _("Settlement recalculated."))
        return redirect("orvahr_benefits:settlement-detail", pk=pk)
    return _form_page(request, form, _("Edit final settlement"), "orvahr_benefits:settlements")


def _settlement_context(settlement):
    return {
        "s": settlement,
        "severance_eligible": settlement.reason in settlement_service.SEVERANCE_REASONS,
        "notice_eligible": settlement.reason in settlement_service.NOTICE_PAY_REASONS,
    }


@login_required
def settlement_detail(request, pk):
    settlement = _settlement_for(request, pk)
    context = _settlement_context(settlement)
    context["is_hr"] = request.user.has_perm(f"{APP}.view_finalsettlement")
    return _no_store(render(request, "orvahr_benefits/settlement_detail.html", context))


@login_required
def settlement_pdf(request, pk):
    from orvahr_th.pdf import ENGINE_XHTML2PDF, html_to_pdf_xhtml2pdf

    settlement = _settlement_for(request, pk)
    context = _settlement_context(settlement)
    context["pdf_engine"] = ENGINE_XHTML2PDF
    html = render_to_string("orvahr_benefits/settlement_pdf.html", context, request=request)
    if request.GET.get("html") == "1":
        return _no_store(HttpResponse(html))
    data = html_to_pdf_xhtml2pdf(html, request_host=request.get_host())
    response = HttpResponse(data, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="final-settlement-{settlement.pk}.pdf"'
    return _no_store(response)


@login_required
@permission_required(f"{APP}.change_finalsettlement")
@require_POST
def settlement_recompute(request, pk):
    settlement = get_object_or_404(FinalSettlement.objects.all(), pk=pk)
    try:
        settlement_service.compute(settlement)
        messages.success(request, _("Settlement recalculated."))
    except settlement_service.SettlementLocked:
        messages.error(request, _("Only a draft settlement can be recalculated."))
    return redirect("orvahr_benefits:settlement-detail", pk=pk)


@login_required
@permission_required(f"{APP}.approve_finalsettlement")
@require_POST
def settlement_approve(request, pk):
    settlement = get_object_or_404(FinalSettlement.objects.all(), pk=pk)
    own = _own_employee(request)
    if own is not None and own.pk == settlement.employee_id:
        raise PermissionDenied
    try:
        settlement_service.approve(settlement, request.user)
        messages.success(request, _("Settlement approved."))
    except settlement_service.SettlementLocked:
        messages.error(request, _("Only a draft settlement can be approved."))
    return redirect("orvahr_benefits:settlement-detail", pk=pk)


@login_required
@permission_required(f"{APP}.change_finalsettlement")
@require_POST
def settlement_mark_paid(request, pk):
    settlement = get_object_or_404(FinalSettlement.objects.all(), pk=pk)
    try:
        settlement_service.mark_paid(settlement)
        messages.success(request, _("Settlement marked as paid."))
    except settlement_service.SettlementLocked:
        messages.error(request, _("Only an approved settlement can be marked as paid."))
    return redirect("orvahr_benefits:settlement-detail", pk=pk)


# ---------------------------------------------------------------- deduction orders


@login_required
@permission_required(f"{APP}.view_deductionorder")
def order_list(request):
    orders = DeductionOrder.objects.all().select_related("employee")
    order_type = request.GET.get("type")
    if order_type in dict(DeductionOrder.TYPE_CHOICES):
        orders = orders.filter(order_type=order_type)
    return render(
        request,
        "orvahr_benefits/orders.html",
        {"orders": orders, "types": DeductionOrder.TYPE_CHOICES, "order_type": order_type},
    )


def _order_edit(request, order):
    form = DeductionOrderForm(request.POST or None, instance=order)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        order_service.sync_order(saved)
        messages.success(request, _("Order saved and synced to payroll."))
        return redirect("orvahr_benefits:orders")
    return _form_page(request, form, _("Deduction order"), "orvahr_benefits:orders")


@login_required
@permission_required(f"{APP}.add_deductionorder")
def order_create(request):
    return _order_edit(request, None)


@login_required
@permission_required(f"{APP}.change_deductionorder")
def order_update(request, pk):
    return _order_edit(request, get_object_or_404(DeductionOrder.objects.all(), pk=pk))


@login_required
@permission_required(f"{APP}.change_deductionorder")
@require_POST
def orders_sync(request):
    summary = order_service.sync_deduction_orders()
    parts = ", ".join(f"{count} {name}" for name, count in sorted(summary.items()))
    messages.success(request, _("Orders synced to payroll: %(parts)s.") % {"parts": parts or "0"})
    return redirect("orvahr_benefits:orders")


def _remittance(request):
    year, month = MonthForm(request.GET).year_month(_default_month())
    order_type = request.GET.get("type")
    if order_type not in dict(DeductionOrder.TYPE_CHOICES):
        order_type = None
    groups, grand = order_service.remittance_report(year, month, order_type)
    visible = set(DeductionOrder.objects.values_list("pk", flat=True))
    for group in groups:
        group["rows"] = [r for r in group["rows"] if r["order"].pk in visible]
        group["total"] = sum((r["amount"] for r in group["rows"]), start=type(grand)("0"))
    groups = [g for g in groups if g["rows"]]
    grand = sum((g["total"] for g in groups), start=type(grand)("0"))
    return year, month, order_type, groups, grand


@login_required
@permission_required(f"{APP}.view_deductionorder")
def remittance_report(request):
    year, month, order_type, groups, grand = _remittance(request)
    return render(
        request,
        "orvahr_benefits/remittance.html",
        {
            "groups": groups,
            "grand": grand,
            "month_value": f"{year:04d}-{month:02d}",
            "types": DeductionOrder.TYPE_CHOICES,
            "order_type": order_type,
        },
    )


@login_required
@permission_required(f"{APP}.view_deductionorder")
def remittance_csv(request):
    year, month, order_type, groups, grand = _remittance(request)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        f'attachment; filename="deduction-remittance-{year:04d}-{month:02d}.csv"'
    )
    response.write("﻿")
    writer = csv.writer(response)
    writer.writerow(
        [
            "order_type",
            "issuer",
            "order_id",
            "reference_masked",
            "badge_id",
            "employee_name",
            "month",
            "amount_deducted",
            "remaining_after",
        ]
    )
    for group in groups:
        for r in group["rows"]:
            writer.writerow(
                [
                    group["order_type_code"],
                    group["issuer"],
                    r["order"].pk,
                    r["masked_reference"],
                    r["employee"].badge_id or "",
                    r["employee"].get_full_name(),
                    f"{year:04d}-{month:02d}",
                    r["amount"],
                    "" if r["remaining"] is None else r["remaining"],
                ]
            )
    return _no_store(response)


# ---------------------------------------------------------------- self-service


@login_required
def my_benefits(request):
    employee = _own_employee(request)
    memberships = []
    settlements = []
    if employee is not None:
        for member in (
            EmployeeProvidentFund.objects.entire()
            .filter(employee=employee, is_active=True)
            .select_related("plan")
        ):
            memberships.append(
                {
                    "member": member,
                    "balance": pvd.member_balance(member),
                    "vesting": pvd.vesting_on_leaving(
                        member, on=member.left_on or datetime.date.today()
                    ),
                }
            )
        settlements = (
            FinalSettlement.objects.entire()
            .filter(employee=employee)
            .exclude(status=FinalSettlement.STATUS_DRAFT)
        )
    return _no_store(
        render(
            request,
            "orvahr_benefits/my_benefits.html",
            {"employee": employee, "memberships": memberships, "settlements": settlements},
        )
    )
