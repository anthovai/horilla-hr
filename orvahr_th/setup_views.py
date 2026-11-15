"""
Views of the Thai company setup wizard (ตั้งค่าบริษัทไทย).

One URL per step: ``setup/<company id>/<step>/``. A GET shows the step; a
POST carries an ``action``: ``save`` (do the step and move on), ``skip``,
or a step-specific one (``holidays`` / ``leave_types`` on the calendar step,
``finish`` on the last). HTMX requests get the wizard body back, so the
stepper updates along with the step; plain requests get the full page (or a
redirect after a POST).

Only superusers and company administrators (``base.change_company``) may
use it, and only for the companies they can see.
"""

from django.http import Http404, HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import NoReverseMatch, reverse
from django.utils.translation import gettext as _

from horilla.decorators import login_required
from orvahr_th import setup_forms as forms
from orvahr_th import setup_wizard as wizard

PAGE = "orvahr_th/setup/wizard.html"
BODY = "orvahr_th/setup/_body.html"
TARGET = "thai-setup"  # id of the element the wizard body is swapped into


def _url(name, *args, query=""):
    try:
        return reverse(name, args=args) + query
    except NoReverseMatch:
        return ""


def _step_url(company, key):
    return reverse("orvahr-th-setup-step", args=[company.pk, key])


def _is_partial(request):
    """An HTMX swap into the wizard (not a boosted or real page navigation)."""
    return (
        request.headers.get("HX-Request") == "true"
        and request.headers.get("HX-Target") == TARGET
        and request.headers.get("Sec-Fetch-Mode") != "navigate"
    )


def _company_or_none(request, company_id):
    return wizard.setup_companies(request.user).filter(pk=company_id).first()


@login_required
def setup_start(request):
    """Resume where this company's setup was left."""
    if not wizard.can_run_setup(request.user):
        return HttpResponseForbidden()
    companies = wizard.setup_companies(request.user)
    company = None
    if request.GET.get("company"):
        try:
            company = companies.filter(pk=int(request.GET["company"])).first()
        except (TypeError, ValueError):
            company = None
    company = company or wizard.default_company(request, companies)
    if company is None:
        raise Http404
    progress = wizard.progress_for(company)
    status = wizard.statuses(company, progress, request.user)
    return redirect(_step_url(company, wizard.resume_key(progress, status)))


@login_required
def setup_step(request, company_id, step):
    if not wizard.can_run_setup(request.user):
        return HttpResponseForbidden()
    if step not in wizard.STEP_KEYS:
        raise Http404
    company = _company_or_none(request, company_id)
    if company is None:
        raise Http404
    progress = wizard.progress_for(company)
    handler = STEP_HANDLERS[step]

    if request.method == "POST":
        action = request.POST.get("action", "save")
        if action == "skip" and step != "finish":
            wizard.record(progress, step, wizard.SKIPPED, request.user, current=wizard.next_key(step))
            return _go(request, company, wizard.next_key(step))
        outcome = handler(request, company, progress, action)
        if outcome.get("next"):
            wizard.record(
                progress, step, wizard.DONE, request.user, current=outcome["next"]
            )
            return _go(request, company, outcome["next"], results=outcome.get("results"))
        return _render(request, company, progress, step, outcome)

    wizard.set_current(progress, step)
    return _render(request, company, progress, step, handler(request, company, progress, None))


def _go(request, company, key, results=None):
    """After a step: the next step (HTMX swaps it in and updates the address bar)."""
    if not _is_partial(request):
        return redirect(_step_url(company, key))
    progress = wizard.progress_for(company)
    wizard.set_current(progress, key)
    outcome = STEP_HANDLERS[key](request, company, progress, None)
    if results:
        outcome["carried_results"] = results
    response = _render(request, company, progress, key, outcome)
    response["HX-Push-Url"] = _step_url(company, key)
    return response


def _render(request, company, progress, step, outcome):
    status = wizard.statuses(company, progress, request.user)
    steps = [
        {
            "key": s.key,
            "number": index,
            "title": s.title,
            "title_th": s.title_th,
            "optional": s.optional,
            "status": status[s.key],
            "url": _step_url(company, s.key),
        }
        for index, s in enumerate(wizard.STEPS, start=1)
    ]
    current = wizard.STEP_BY_KEY[step]
    context = {
        "company": company,
        "companies": wizard.setup_companies(request.user),
        "progress": progress,
        "steps": steps,
        "step": current,
        "step_number": wizard.STEP_KEYS.index(step) + 1,
        "step_count": len(wizard.STEP_KEYS),
        "step_status": status[step],
        "step_url": _step_url(company, step),
        "previous_url": _step_url(company, wizard.previous_key(step)) if step != wizard.STEP_KEYS[0] else "",
        "step_template": f"orvahr_th/setup/step_{step}.html",
        "done_count": sum(1 for s in steps if s["status"] == wizard.DONE),
        **outcome,
    }
    template = BODY if _is_partial(request) else PAGE
    response = render(request, template, context)
    response["Cache-Control"] = "no-store, private"
    return response


# ------------------------------------------------------------------ steps
# Each handler returns a context dict. A POST that completed the step sets
# "next" (the step to go to) and "results" (what was done, shown there).


def step_company(request, company, progress, action):
    from orvahr_filing.models import profile_for

    instance = profile_for(company)
    if action == "save":
        form = forms.CompanyStepForm(request.POST, instance=instance)
        if form.is_valid():
            wizard.save_tax_profile(company, form)
            return {"next": "payroll", "results": [_("Company tax profile saved.")]}
    else:
        initial = {} if instance else {"legal_name_th": company.company}
        form = forms.CompanyStepForm(instance=instance, initial=initial)
    return {
        "form": form,
        "profile": instance,
        "full_profile_url": _url("orvahr_filing:tax-profile", query=f"?company={company.pk}"),
    }


def step_payroll(request, company, progress, action):
    if action == "save":
        form = forms.PayrollStepForm(request.POST)
        if form.is_valid():
            results = wizard.apply_payroll(company, progress, form.cleaned_data)
            return {"next": "bank", "results": results}
    else:
        period = wizard.pay_period(company)
        visited = bool((progress.steps or {}).get("payroll"))
        initial = {
            "use_baht": True,
            "thai_tax_pack": bool(progress.default_filing_status_id) if visited else True,
            "load_sso": True,
            "pay_day": str(period.pay_day_of_month) if period and period.pay_day_of_month else "",
        }
        form = forms.PayrollStepForm(initial=initial)
    rows = {row["key"]: row for row in form.ot_rows}
    return {
        "form": form,
        "ot_options": [
            {"key": key, "label": label, "row": rows.get(key), "checked": key in (form["ot_keys"].value() or [])}
            for key, label in form.fields["ot_keys"].choices
        ],
        "sso": wizard.sso_deduction(),
        "tax_pack": wizard.thai_tax_pack(),
        "currency": wizard.payroll_settings(company),
        "pay_period": wizard.pay_period(company),
        "default_filing_status": progress.default_filing_status,
        "component_library_url": _url("component-library"),
        "pay_period_url": _url("pay-period-settings"),
    }


def step_bank(request, company, progress, action):
    from orvahr_bank.models import CompanyBankAccount

    accounts = list(wizard.bank_accounts(company))
    instance = accounts[0] if accounts else CompanyBankAccount(company_id=company)
    if action == "save":
        form = forms.BankStepForm(request.POST, instance=instance)
        if form.is_valid():
            wizard.save_bank_account(company, form)
            return {"next": "calendar", "results": [_("Payroll bank account saved.")]}
    elif action == "continue" and accounts:
        return {"next": "calendar", "results": []}
    else:
        form = forms.BankStepForm(instance=instance)
    return {
        "form": form,
        "accounts": accounts,
        "accounts_url": _url("orvahr_bank:accounts"),
    }


def step_calendar(request, company, progress, action):
    from django.utils import timezone

    rows = wizard.leave_type_rows(company)
    holiday_form = forms.HolidayStepForm()
    leave_form = forms.LeaveTypesStepForm(rows=rows)
    context = {}
    if action == "holidays":
        holiday_form = forms.HolidayStepForm(request.POST, request.FILES)
        if holiday_form.is_valid():
            year = holiday_form.cleaned_data["year"]
            result = wizard.load_holidays(company, year, holiday_form.cleaned_data.get("lunar_csv"))
            context["holiday_result"] = result
            context["holiday_year"] = year
    elif action == "leave_types":
        leave_form = forms.LeaveTypesStepForm(request.POST, rows=rows)
        if leave_form.is_valid():
            created = wizard.create_leave_types(company, leave_form.cleaned_data["leave_types"])
            context["leave_created"] = created
            context["leave_saved"] = True
            rows = wizard.leave_type_rows(company)
            leave_form = forms.LeaveTypesStepForm(rows=rows)
    elif action == "continue":
        return {"next": "benefits", "results": []}

    year = timezone.localdate().year
    holidays = list(wizard.holidays_in_year(company, year))
    next_year_holidays = wizard.holidays_in_year(company, year + 1).count()
    return {
        **context,
        "holiday_form": holiday_form,
        "leave_form": leave_form,
        "leave_rows": rows,
        "this_year": year,
        "holidays": holidays,
        "next_year_holiday_count": next_year_holidays,
        "lunar_labels": wizard.lunar_holiday_labels(),
        "lunar_template_url": reverse("orvahr-th-setup-lunar-csv", args=[company.pk]),
        "holidays_url": _url("holiday-view"),
        "leave_types_url": _url("type-view"),
    }


def step_benefits(request, company, progress, action):
    plans = list(wizard.pvd_plans(company))
    instance = plans[0] if plans else None
    if action == "save":
        form = forms.PvdStepForm(request.POST, instance=instance)
        if form.is_valid():
            wizard.save_pvd_plan(company, form)
            return {"next": "pdpa", "results": [_("Provident fund plan saved.")]}
    else:
        form = forms.PvdStepForm(instance=instance)
    return {
        "form": form,
        "plans": plans,
        "plan_edit_url": _url("orvahr_benefits:pvd-plan-update", instance.pk) if instance else "",
    }


def step_pdpa(request, company, progress, action):
    instance = wizard.pdpa_settings(company)
    if action == "save":
        form = forms.DpoStepForm(request.POST, instance=instance)
        notice_form = forms.NoticeStepForm(request.POST)
        if form.is_valid() and notice_form.is_valid():
            wizard.save_dpo(company, form)
            published = wizard.publish_notices(company, notice_form.cleaned_data["audiences"])
            results = [_("Data Protection Officer contact saved.")]
            if published:
                results.append(
                    _("Privacy notice published as a DRAFT: have counsel review and edit it.")
                )
            return {"next": "people", "results": results}
    else:
        form = forms.DpoStepForm(instance=instance)
        notice_form = forms.NoticeStepForm()
    notices = [
        {
            "notice": notice,
            "draft": wizard.is_draft_notice(notice),
            "edit_url": _url("orvahr_pdpa:notice-update", notice.pk),
        }
        for notice in wizard.company_notices(company)
    ]
    return {
        "form": form,
        "notice_form": notice_form,
        "notices": notices,
        "pdpa_settings_url": _url("orvahr_pdpa:settings"),
    }


def step_people(request, company, progress, action):
    if action in ("save", "continue"):
        return {"next": "finish", "results": []}
    return {
        "employee_count": wizard.employee_count(company, exclude_user=request.user),
        "import_url": _url("master-import", query="?dataset=employees"),
        "template_url": reverse("orvahr-th-setup-import-template", args=[company.pk]),
        "add_employee_url": _url("employee-view-new"),
    }


def step_finish(request, company, progress, action):
    context = {}
    if action == "finish":
        wizard.finish(progress, request.user)
        context["just_finished"] = True
    status = wizard.statuses(company, progress, request.user)
    from orvahr_filing.models import profile_for

    return {
        **context,
        "summary": [
            {"key": s.key, "title": s.title, "title_th": s.title_th, "optional": s.optional,
             "status": status[s.key], "url": _step_url(company, s.key)}
            for s in wizard.STEPS
            if s.key != "finish"
        ],
        "profile": profile_for(company),
        "checks": wizard.ACCOUNTANT_CHECKS,
        "draft_notices": [n for n in wizard.company_notices(company) if wizard.is_draft_notice(n)],
        "dashboard_url": _url("home-page") or "/",
    }


STEP_HANDLERS = {
    "company": step_company,
    "payroll": step_payroll,
    "bank": step_bank,
    "calendar": step_calendar,
    "benefits": step_benefits,
    "pdpa": step_pdpa,
    "people": step_people,
    "finish": step_finish,
}


# ------------------------------------------------------------- downloads


@login_required
def import_template(request, company_id):
    if not wizard.can_run_setup(request.user):
        return HttpResponseForbidden()
    company = _company_or_none(request, company_id)
    if company is None:
        raise Http404
    response = HttpResponse(
        wizard.thai_import_template(company),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = 'attachment; filename="employees_thai_template.xlsx"'
    return response


@login_required
def lunar_csv(request, company_id):
    if not wizard.can_run_setup(request.user):
        return HttpResponseForbidden()
    if _company_or_none(request, company_id) is None:
        raise Http404
    response = HttpResponse(wizard.lunar_csv_template(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="thai_lunar_holidays.csv"'
    return response
