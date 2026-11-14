"""
Withholding tax certificate (50 ทวิ) DRAFT: HTML and PDF.

Who may see it: anyone with payroll's view-payslip permission, or the
employee it is about. Nobody else -- not even someone who can see the
employee's profile -- because it carries their tax ID and a year's pay.

The URL carries the employee's database id and the year, never the tax ID.
"""

from django.http import HttpResponse, HttpResponseForbidden, HttpResponseNotFound
from django.template.loader import render_to_string

from employee.models import Employee
from horilla.decorators import login_required
from orvahr_th.pdf import ENGINE_XHTML2PDF, html_to_pdf_xhtml2pdf
from orvahr_th.withholding import (
    DRAFT_NOTICE_EN,
    DRAFT_NOTICE_TH,
    build_certificate,
)

TEMPLATE = "orvahr_th/wht_certificate.html"
MIN_YEAR, MAX_YEAR = 2000, 2100


def _may_view(user, employee):
    if user.has_perm("payroll.view_payslip"):
        return True
    return employee.employee_user_id_id is not None and (
        employee.employee_user_id_id == user.pk
    )


def _no_store(response):
    # Pay and a tax ID: keep it out of shared and browser caches.
    response["Cache-Control"] = "no-store, private"
    return response


@login_required
def wht_certificate(request, employee_id, year, pdf=False):
    if not MIN_YEAR <= year <= MAX_YEAR:
        return HttpResponseNotFound()
    employee = Employee.objects.entire().filter(pk=employee_id).first()
    if employee is None:
        return HttpResponseNotFound()
    if not _may_view(request.user, employee):
        return HttpResponseForbidden()

    cert = build_certificate(employee, year)
    context = {
        "cert": cert,
        "draft_notice_th": DRAFT_NOTICE_TH,
        "draft_notice_en": DRAFT_NOTICE_EN,
        "pdf_engine": ENGINE_XHTML2PDF if pdf else None,
    }
    html = render_to_string(TEMPLATE, context, request=request)
    if not pdf:
        return _no_store(HttpResponse(html))
    data = html_to_pdf_xhtml2pdf(html, request_host=request.get_host())
    response = HttpResponse(data, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'inline; filename="wht-50-tawi-draft-{employee.pk}-{year}.pdf"'
    )
    return _no_store(response)


def wht_certificate_pdf(request, employee_id, year):
    return wht_certificate(request, employee_id, year, pdf=True)
