"""
"Thai Info" tab of the employee profile: EmployeeThaiProfile and the
EmployeeTaxClaim rows for a tax year.

Permission model
----------------
* view: ``employee.view_employee`` or ``employee.change_employee``, or the
  employee looking at their own profile. Everyone else gets 403.
* edit (profile and claims): ``employee.change_employee`` only.
* National ID, tax ID, SSO number and PromptPay ID are shown in full only to
  ``employee.change_employee``; everyone else sees them masked.

PDPA: the sensitive values never go into URLs, GET parameters, messages or
logs. URLs carry only the employee/claim primary key and the tax year.
"""

from datetime import date

from django.contrib import messages
from django.http import Http404
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_http_methods, require_POST

from employee.forms import EmployeeTaxClaimForm, EmployeeThaiProfileForm
from employee.models import Employee, EmployeeTaxClaim, EmployeeThaiProfile
from employee.thai_validators import format_thai_id, mask_id
from horilla.decorators import hx_request_required, login_required

EDIT_PERM = "employee.change_employee"
VIEW_PERM = "employee.view_employee"
TAB_TEMPLATE = "tabs/thai_info_tab.html"
PROFILE_FORM_TEMPLATE = "tabs/thai_info_profile_form.html"
CLAIM_FORM_TEMPLATE = "tabs/thai_info_claim_form.html"
MIN_TAX_YEAR = 2000
MAX_TAX_YEAR = 2200


def can_edit_thai_info(user):
    return user.is_authenticated and user.has_perm(EDIT_PERM)


def can_view_thai_info(user, employee):
    if not user.is_authenticated or employee is None:
        return False
    if user.has_perm(EDIT_PERM) or user.has_perm(VIEW_PERM):
        return True
    return employee.employee_user_id_id == user.pk


def _forbidden(request):
    return render(request, "decorator_404.html", status=403)


def _get_employee(pk):
    # entire(): the profile page itself resolves the employee with entire().
    employee = Employee.objects.entire().filter(pk=pk).first()
    if employee is None:
        raise Http404
    return employee


def _get_profile(employee):
    # _base_manager: the default manager is company-scoped.
    return EmployeeThaiProfile._base_manager.filter(employee_id=employee).first()


def current_tax_year():
    """Thai personal income tax years are calendar years."""
    return date.today().year


def _parse_year(value):
    try:
        year = int(value)
    except (TypeError, ValueError):
        return current_tax_year()
    if MIN_TAX_YEAR <= year <= MAX_TAX_YEAR:
        return year
    return current_tax_year()


def _profile_rows(profile, unmasked):
    """Label/value pairs for display; sensitive values masked unless unmasked."""
    if profile is None:
        return []

    def id_value(value):
        return format_thai_id(value, masked=not unmasked)

    def plain_or_masked(value):
        if not value:
            return ""
        return value if unmasked else mask_id(value)

    full_name_th = " ".join(
        part
        for part in (
            profile.get_name_title_display() if profile.name_title else "",
            profile.first_name_th,
            profile.last_name_th,
        )
        if part
    )
    address = ", ".join(
        part
        for part in (
            profile.address_line,
            profile.subdistrict,
            profile.district,
            profile.province,
            profile.postcode,
        )
        if part
    )
    return [
        (_("Name (Thai)"), full_name_th),
        (_("National ID"), id_value(profile.national_id)),
        (_("Tax ID"), id_value(profile.tax_id)),
        (_("Social security number"), plain_or_masked(profile.social_security_no)),
        (_("SSO registered hospital"), profile.sso_hospital),
        (_("Bank"), profile.get_bank_code_display() if profile.bank_code else ""),
        (_("PromptPay ID"), plain_or_masked(profile.promptpay_id)),
        (_("Address"), address),
    ]


def _render_tab(request, employee, year=None, status=200):
    year = year or _parse_year(request.GET.get("year"))
    can_edit = can_edit_thai_info(request.user)
    profile = _get_profile(employee)
    claims = EmployeeTaxClaim.objects.filter(
        employee_id=employee, tax_year=year
    ).order_by("code")
    this_year = current_tax_year()
    years = sorted(
        set(
            EmployeeTaxClaim.objects.filter(employee_id=employee).values_list(
                "tax_year", flat=True
            )
        )
        | {this_year - 1, this_year, this_year + 1, year},
        reverse=True,
    )
    return render(
        request,
        TAB_TEMPLATE,
        {
            "employee": employee,
            "has_profile": profile is not None,
            "profile_rows": _profile_rows(profile, unmasked=can_edit),
            "ids_masked": not can_edit,
            "claims": claims,
            "year": year,
            "years": years,
            "can_edit": can_edit,
        },
        status=status,
    )


@login_required
@hx_request_required
def thai_info_tab(request, pk, **kwargs):
    """The tab body. GET ?year=YYYY switches the tax year shown."""
    employee = _get_employee(pk)
    if not can_view_thai_info(request.user, employee):
        return _forbidden(request)
    return _render_tab(request, employee)


@login_required
@hx_request_required
@require_http_methods(["GET", "POST"])
def thai_profile_edit(request, pk):
    """Create or update the employee's Thai profile (HR only)."""
    employee = _get_employee(pk)
    if not can_edit_thai_info(request.user):
        return _forbidden(request)
    instance = _get_profile(employee) or EmployeeThaiProfile(employee_id=employee)
    if request.method == "POST":
        form = EmployeeThaiProfileForm(request.POST, instance=instance)
        if form.is_valid():
            form.save()
            messages.success(request, _("Thai information saved."))
            return _render_tab(request, employee)
    else:
        form = EmployeeThaiProfileForm(instance=instance)
    return render(request, PROFILE_FORM_TEMPLATE, {"employee": employee, "form": form})


def _claim_form_response(request, employee, form, claim=None):
    return render(
        request,
        CLAIM_FORM_TEMPLATE,
        {"employee": employee, "form": form, "claim": claim},
    )


@login_required
@hx_request_required
@require_http_methods(["GET", "POST"])
def tax_claim_add(request, pk):
    """Add a tax allowance claim for the employee (HR only)."""
    employee = _get_employee(pk)
    if not can_edit_thai_info(request.user):
        return _forbidden(request)
    if request.method == "POST":
        form = EmployeeTaxClaimForm(request.POST, employee=employee)
        if form.is_valid():
            claim = form.save()
            messages.success(request, _("Tax allowance claim added."))
            return _render_tab(request, employee, year=claim.tax_year)
    else:
        form = EmployeeTaxClaimForm(
            employee=employee,
            initial={"tax_year": _parse_year(request.GET.get("year"))},
        )
    return _claim_form_response(request, employee, form)


@login_required
@hx_request_required
@require_http_methods(["GET", "POST"])
def tax_claim_edit(request, claim_id):
    """Edit one tax allowance claim (HR only)."""
    if not can_edit_thai_info(request.user):
        return _forbidden(request)
    claim = EmployeeTaxClaim.objects.filter(pk=claim_id).first()
    if claim is None:
        raise Http404
    employee = claim.employee_id
    if request.method == "POST":
        form = EmployeeTaxClaimForm(request.POST, instance=claim, employee=employee)
        if form.is_valid():
            claim = form.save()
            messages.success(request, _("Tax allowance claim updated."))
            return _render_tab(request, employee, year=claim.tax_year)
    else:
        form = EmployeeTaxClaimForm(instance=claim, employee=employee)
    return _claim_form_response(request, employee, form, claim)


@login_required
@hx_request_required
@require_POST
def tax_claim_delete(request, claim_id):
    """Delete one tax allowance claim (HR only)."""
    if not can_edit_thai_info(request.user):
        return _forbidden(request)
    claim = EmployeeTaxClaim.objects.filter(pk=claim_id).first()
    if claim is None:
        raise Http404
    employee, year = claim.employee_id, claim.tax_year
    claim.delete()
    messages.success(request, _("Tax allowance claim deleted."))
    return _render_tab(request, employee, year=year)
