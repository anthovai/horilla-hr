"""
Statutory filing screens: company tax profile (settings), ภ.ง.ด.1, ภ.ง.ด.1ก,
สปส.1-10 and the joiner / leaver lists.

Access: the reports need payroll's view-payslip permission (they carry every
employee's pay); the tax profile needs this app's own model permissions.
Every response that carries pay or an ID is ``no-store``.

URLs: the company (database id) and the period are query parameters; no tax
ID, national ID or SSO number ever appears in a URL, and the download file
names carry only the period and the company's database id.
"""

import datetime

from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.translation import gettext as _

from horilla.decorators import login_required, permission_required
from orvahr_filing import common, movements, pnd1, rd_file, sso
from orvahr_filing.forms import CompanyTaxProfileForm
from orvahr_filing.models import profile_for
from orvahr_th.pdf import ENGINE_XHTML2PDF, html_to_pdf_xhtml2pdf
from orvahr_th.withholding import DRAFT_NOTICE_EN, DRAFT_NOTICE_TH

PAYROLL_VIEW = "payroll.view_payslip"
MIN_YEAR, MAX_YEAR = 2000, 2100


def _no_store(response):
    response["Cache-Control"] = "no-store, private"
    return response


def _previous_month(today=None):
    today = today or datetime.date.today()
    first = today.replace(day=1)
    last = first - datetime.timedelta(days=1)
    return last.year, last.month


def _month(request):
    """``?period=YYYY-MM``, defaulting to last month (what is filed now)."""
    raw = request.GET.get("period") or ""
    try:
        year, month = (int(part) for part in raw.split("-"))
        if MIN_YEAR <= year <= MAX_YEAR and 1 <= month <= 12:
            return year, month
    except ValueError:
        pass
    return _previous_month()


def _year(request):
    """``?year=YYYY`` (AD), defaulting to last year (what PND 1 Kor is filed for)."""
    raw = request.GET.get("year") or ""
    if raw.isdigit() and MIN_YEAR <= int(raw) <= MAX_YEAR:
        return int(raw)
    return datetime.date.today().year - 1


def _scope(request):
    companies = common.reportable_companies(request)
    company = common.selected_company(request, companies)
    return companies, company


def _base_context(request, companies, company, **extra):
    context = {
        "companies": companies,
        "company": company,
        "profile": profile_for(company)
        if company
        else None,
        "can_edit_profile": request.user.has_perm("orvahr_filing.change_companytaxprofile")
        or request.user.has_perm("orvahr_filing.add_companytaxprofile"),
    }
    context.update(extra)
    return context


def _query(company, **params):
    from urllib.parse import urlencode

    data = {"company": company.pk} if company else {}
    data.update({k: v for k, v in params.items() if v not in (None, "")})
    return urlencode(data)


def _attachment(response, filename):
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# ---------------------------------------------------------------- settings


@login_required
@permission_required("orvahr_filing.view_companytaxprofile")
def tax_profile_settings(request):
    companies, company = _scope(request)
    instance = profile_for(company) if company else None
    can_edit = request.user.has_perm(
        "orvahr_filing.change_companytaxprofile" if instance else "orvahr_filing.add_companytaxprofile"
    )
    form = None
    if company is not None:
        form = CompanyTaxProfileForm(
            request.POST if request.method == "POST" else None, instance=instance
        )
        if request.method == "POST":
            if not can_edit:
                messages.error(request, _("You don't have permission."))
                return redirect(f"{reverse('orvahr_filing:tax-profile')}?{_query(company)}")
            if form.is_valid():
                profile = form.save(commit=False)
                profile.company = company
                profile.save()
                messages.success(request, _("Company tax profile saved."))
                return redirect(f"{reverse('orvahr_filing:tax-profile')}?{_query(company)}")
        if not can_edit:
            for field in form.fields.values():
                field.disabled = True
    return _no_store(
        render(
            request,
            "orvahr_filing/tax_profile.html",
            {
                "companies": companies,
                "company": company,
                "form": form,
                "instance": instance,
                "can_edit": can_edit,
            },
        )
    )


# ---------------------------------------------------------------- PND 1


def _pnd_pdf(request, report, filename):
    html = render_to_string(
        "orvahr_filing/pnd_attachment_pdf.html",
        {
            "report": report,
            "draft_notice_th": DRAFT_NOTICE_TH,
            "draft_notice_en": DRAFT_NOTICE_EN,
            "pdf_engine": ENGINE_XHTML2PDF,
        },
        request=request,
    )
    data = html_to_pdf_xhtml2pdf(html, request_host=request.get_host())
    response = HttpResponse(data, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return _no_store(response)


@login_required
@permission_required(PAYROLL_VIEW)
def pnd1_monthly(request, output="html"):
    companies, company = _scope(request)
    year, month = _month(request)
    withheld_only = request.GET.get("withheld_only") == "1"
    report = pnd1.build_monthly(company, year, month, withheld_only) if company else None
    query = _query(company, period=f"{year:04d}-{month:02d}", withheld_only="1" if withheld_only else "")

    if report is not None and output == "pdf":
        return _pnd_pdf(request, report, f"pnd1-draft-{company.pk}-{year:04d}{month:02d}.pdf")
    file_problems = None
    if report is not None and output == "rd":
        response, file_problems = _rd_download(report)
        if response is not None:
            return response

    return _no_store(
        render(
            request,
            "orvahr_filing/pnd1.html",
            _base_context(
                request,
                companies,
                company,
                report=report,
                kind=pnd1.PND1,
                period=f"{year:04d}-{month:02d}",
                withheld_only=withheld_only,
                query=query,
                efiling_url=reverse("orvahr_filing:pnd1-rd"),
                file_problems=file_problems,
                spec=rd_file,
            ),
        )
    )


@login_required
@permission_required(PAYROLL_VIEW)
def pnd1_annual(request, output="html"):
    companies, company = _scope(request)
    year = _year(request)
    withheld_only = request.GET.get("withheld_only") == "1"
    report = pnd1.build_annual(company, year, withheld_only) if company else None
    query = _query(company, year=year, withheld_only="1" if withheld_only else "")

    if report is not None and output == "pdf":
        return _pnd_pdf(request, report, f"pnd1k-draft-{company.pk}-{year:04d}.pdf")
    file_problems = None
    if report is not None and output == "rd":
        response, file_problems = _rd_download(report)
        if response is not None:
            return response

    return _no_store(
        render(
            request,
            "orvahr_filing/pnd1.html",
            _base_context(
                request,
                companies,
                company,
                report=report,
                kind=pnd1.PND1K,
                year=year,
                withheld_only=withheld_only,
                query=query,
                efiling_url=reverse("orvahr_filing:pnd1k-rd"),
                file_problems=file_problems,
                spec=rd_file,
            ),
        )
    )


def _rd_download(report):
    """(response, None) with the RD file, or (None, problems) when not ready."""
    try:
        built = rd_file.build(report)
    except rd_file.NotReady as exc:
        return None, exc.problems
    response = HttpResponse(built.content, content_type="text/plain; charset=utf-8")
    return _no_store(_attachment(response, built.name)), None


def pnd1_monthly_rd(request):
    return pnd1_monthly(request, output="rd")


def pnd1_annual_rd(request):
    return pnd1_annual(request, output="rd")


def pnd1_monthly_pdf(request):
    return pnd1_monthly(request, output="pdf")


def pnd1_annual_pdf(request):
    return pnd1_annual(request, output="pdf")


# ---------------------------------------------------------------- SSO


@login_required
@permission_required(PAYROLL_VIEW)
def sso_monthly(request):
    companies, company = _scope(request)
    year, month = _month(request)
    report = sso.build(company, year, month) if company else None
    joined = movements.joiners(company, year, month) if company else []
    left = movements.leavers(company, year, month) if company else []
    return _no_store(
        render(
            request,
            "orvahr_filing/sso.html",
            _base_context(
                request,
                companies,
                company,
                report=report,
                joined=joined,
                left=left,
                period=f"{year:04d}-{month:02d}",
                query=_query(company, period=f"{year:04d}-{month:02d}"),
                sso_file_url=reverse("orvahr_filing:sso-csv"),
                sso_file_label=_("Part 2 as CSV (generic, not the SSO upload format)"),
            ),
        )
    )


@login_required
@permission_required(PAYROLL_VIEW)
def sso_csv(request):
    """
    Part 2 as a GENERIC CSV. Not the SSO upload format: no official SSO
    text-file specification could be obtained, so none is imitated here.
    """
    import csv
    import io

    companies, company = _scope(request)
    if company is None:
        return redirect(reverse("orvahr_filing:sso"))
    year, month = _month(request)
    report = sso.build(company, year, month)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["seq", "sso_number", "title", "first_name", "last_name", "wage",
         "contribution_base", "employee_contribution", "employer_contribution", "source"]
    )
    for line in report.lines:
        writer.writerow(
            [line.seq, line.payee.sso_no, line.payee.title, line.payee.first_name,
             line.payee.last_name, line.wage, line.base, line.employee_amount,
             line.employer_amount, line.source]
        )
    writer.writerow(
        ["total", "", "", "", "", report.total_wage, report.total_base,
         report.total_employee, report.total_employer, ""]
    )
    response = HttpResponse("\ufeff" + buffer.getvalue(), content_type="text/csv; charset=utf-8")
    return _no_store(
        _attachment(response, f"sso-1-10-generic-{company.pk}-{year:04d}{month:02d}.csv")
    )


@login_required
@permission_required(PAYROLL_VIEW)
def sso_movements_csv(request):
    companies, company = _scope(request)
    if company is None:
        return redirect(reverse("orvahr_filing:sso"))
    year, month = _month(request)
    body = movements.to_csv(
        movements.joiners(company, year, month), movements.leavers(company, year, month)
    )
    response = HttpResponse(body, content_type="text/csv; charset=utf-8")
    return _no_store(
        _attachment(response, f"sso-joiners-leavers-{company.pk}-{year:04d}{month:02d}.csv")
    )


@login_required
@permission_required(PAYROLL_VIEW)
def filing_home(request):
    return redirect(reverse("orvahr_filing:pnd1"))
