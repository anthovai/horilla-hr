"""
Payroll bank transfer files.

Export page: pick a payroll run and a company account, preview who is paid and
the total (problems listed by name and reason), then download. The file is
built by services/exports; this app gathers the data and keeps an export log.

Who: payroll's change-payslip permission for exporting; the model's own
permissions for managing company accounts. Every queryset is company-scoped
through HorillaCompanyManager, and a run and an account from different
companies are refused.
"""

import logging

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _

from horilla.decorators import login_required, permission_required
from orvahr_bank.forms import BankExportForm, CompanyBankAccountForm
from orvahr_bank.models import BankExportLog, CompanyBankAccount
from orvahr_bank.services import build_pay_batch, request_export

logger = logging.getLogger(__name__)

EXPORT_TEMPLATE = "orvahr_bank/export.html"
ACCOUNTS_TEMPLATE = "orvahr_bank/accounts.html"
ACCOUNT_FORM_TEMPLATE = "orvahr_bank/account_form.html"


def _no_store(response):
    response["Cache-Control"] = "no-store, private"
    return response


def _render_export(request, form, built=None, error="", service_problems=None):
    context = {
        "form": form,
        "built": built,
        "error": error,
        "service_problems": service_problems or [],
        "logs": BankExportLog.objects.select_related(
            "payroll_batch", "bank_account", "created_by"
        )[:10],
        "has_accounts": CompanyBankAccount.objects.filter(is_active=True).exists(),
    }
    return _no_store(render(request, EXPORT_TEMPLATE, context))


@login_required
@permission_required("payroll.change_payslip")
def bank_export(request):
    if request.method != "POST":
        return _render_export(request, BankExportForm())

    form = BankExportForm(request.POST)
    if not form.is_valid():
        return _render_export(request, form)

    data = form.cleaned_data
    batch, account = data["payroll_batch"], data["bank_account"]
    built = build_pay_batch(batch, account, data["effective_date"])

    if request.POST.get("action") != "download":
        return _render_export(request, _with_fingerprint(form, built), built)

    # Download: everything is rebuilt from the database, never taken from the
    # browser. If the run changed since the preview, show the new preview.
    if data.get("fingerprint") != built.fingerprint():
        return _render_export(
            request,
            _with_fingerprint(form, built),
            built,
            error=_("The run changed since the preview. Check the new totals."),
        )
    if built.batch_problems:
        return _render_export(request, form, built)
    if built.problem_rows and not data.get("exclude_problems"):
        return _render_export(
            request,
            form,
            built,
            error=_(
                "Some employees cannot be paid by file. Fix them, or confirm "
                "exporting without them."
            ),
        )
    if not built.line_count:
        return _render_export(
            request, form, built, error=_("There is nobody to transfer to.")
        )

    exporter_id = account.default_exporter
    result = request_export(exporter_id, built)
    if not result.ok:
        return _render_export(
            request, form, built, error=result.error, service_problems=result.problems
        )

    BankExportLog.objects.create(
        company_id=account.company_id,
        payroll_batch=batch,
        bank_account=account,
        exporter_id=exporter_id,
        period=built.payload["period"],
        effective_date=data["effective_date"],
        line_count=built.line_count,
        excluded_count=len(built.problem_rows),
        total_satang=built.total_satang,
        filename=result.filename,
        created_by=request.user,
    )
    response = HttpResponse(result.data, content_type=result.content_type)
    response["Content-Disposition"] = f'attachment; filename="{result.filename}"'
    response["X-Content-Type-Options"] = "nosniff"
    return _no_store(response)


def _with_fingerprint(form, built):
    """A bound copy of ``form`` carrying the fingerprint of this preview."""
    post = form.data.copy()
    post["fingerprint"] = built.fingerprint()
    new = BankExportForm(post)
    new.is_valid()
    return new


# ------------------------------------------------------------ accounts


@login_required
@permission_required("orvahr_bank.view_companybankaccount")
def account_list(request):
    accounts = CompanyBankAccount.objects.select_related("company_id")
    return _no_store(render(request, ACCOUNTS_TEMPLATE, {"accounts": accounts}))


@login_required
@permission_required("orvahr_bank.add_companybankaccount")
def account_create(request):
    return _account_form(request, CompanyBankAccount())


@login_required
@permission_required("orvahr_bank.change_companybankaccount")
def account_update(request, pk):
    return _account_form(request, get_object_or_404(CompanyBankAccount, pk=pk))


def _account_form(request, instance):
    form = CompanyBankAccountForm(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("Bank account saved."))
        return redirect("orvahr_bank:accounts")
    return _no_store(render(request, ACCOUNT_FORM_TEMPLATE, {"form": form}))
