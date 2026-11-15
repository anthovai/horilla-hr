"""
PDPA views. Function views + plain forms, same shape as line_integration.

Permissions (all bundled in the "PDPA Officer" group, see apps.py):
* view_/add_/change_ on each model gate the matching pages;
* orvahr_pdpa.export_personal_data gates the access/portability export;
* anonymisation additionally requires a superuser.
Self-service (/pdpa/my-privacy/) needs only a login and an employee record.
"""

from datetime import datetime, time, timedelta

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from employee.models import Employee
from horilla.decorators import (
    any_permission_required,
    login_required,
    permission_required,
    superuser_required,
)
from orvahr_pdpa import consent as consent_service
from orvahr_pdpa import retention
from orvahr_pdpa.access_log import log_sensitive_access
from orvahr_pdpa.export import build_zip
from orvahr_pdpa.forms import (
    AnonymiseConfirmForm,
    BreachIncidentForm,
    ConsentPurposeForm,
    DataSubjectRequestForm,
    DsrUpdateForm,
    PdpaSettingsForm,
    PrivacyNoticeForm,
    RetentionPolicyForm,
)
from orvahr_pdpa.models import (
    BreachIncident,
    ConsentPurpose,
    ConsentRecord,
    DataSubjectRequest,
    PdpaSettings,
    PrivacyNotice,
    RetentionPolicy,
    SensitiveAccessLog,
    employee_company,
)
from orvahr_pdpa.notify import notify_dsr_event

OFFICER_PERMS = [
    "orvahr_pdpa.pdpa_officer",
    "orvahr_pdpa.view_datasubjectrequest",
    "orvahr_pdpa.view_breachincident",
    "orvahr_pdpa.view_consentrecord",
    "orvahr_pdpa.view_sensitiveaccesslog",
    "orvahr_pdpa.view_retentionpolicy",
]
PAGE_SIZE = 50


def _page(request, qs):
    return Paginator(qs, PAGE_SIZE).get_page(request.GET.get("page"))


def _query_without_page(request):
    params = request.GET.copy()
    params.pop("page", None)
    return params.urlencode()


def _form_page(request, form, title, back_url):
    return render(request, "orvahr_pdpa/form.html", {"form": form, "title": title, "back_url": back_url})


# Dashboard ---------------------------------------------------------------


@login_required
@any_permission_required(OFFICER_PERMS)
def dashboard(request):
    now = timezone.now()
    open_dsr = DataSubjectRequest.objects.filter(status__in=DataSubjectRequest.OPEN_STATUSES)
    breaches = BreachIncident.objects.exclude(status="closed")
    return render(
        request,
        "orvahr_pdpa/dashboard.html",
        {
            "open_dsr_count": open_dsr.count(),
            "overdue_count": open_dsr.filter(due_at__lt=now).count(),
            "overdue_dsr": open_dsr.filter(due_at__lt=now).select_related("employee", "candidate", "assignee")[:20],
            "due_soon_dsr": open_dsr.filter(due_at__gte=now, due_at__lt=now + timedelta(days=7)).count(),
            "breaches": [b for b in breaches if b.awaiting_report][:20],
            "open_breach_count": breaches.count(),
            "dpo": PdpaSettings.for_company(_selected_company(request)),
        },
    )


def _selected_company(request):
    from horilla.horilla_middlewares import get_selected_company

    company_id = get_selected_company()
    if not company_id or company_id == "all":
        return None
    from base.models import Company

    return Company.objects.filter(pk=company_id).first()


# Settings: DPO, notices, purposes, retention policies ---------------------


@login_required
@permission_required("orvahr_pdpa.view_pdpasettings")
def settings_view(request):
    return render(
        request,
        "orvahr_pdpa/settings.html",
        {
            "settings_rows": PdpaSettings.objects.select_related("company"),
            "notices": PrivacyNotice.objects.select_related("company"),
            "purposes": ConsentPurpose.objects.select_related("company"),
            "policies": RetentionPolicy.objects.select_related("company"),
        },
    )


def _crud(request, form_class, model, pk, title, perm_add, perm_change, back="orvahr_pdpa:settings"):
    instance = get_object_or_404(model.objects, pk=pk) if pk else None
    if not request.user.has_perm(perm_change if instance else perm_add):
        return render(request, "decorator_404.html", status=403)
    form = form_class(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("Saved."))
        return redirect(back)
    return _form_page(request, form, title, reverse(back))


@login_required
def settings_form(request, pk=None):
    return _crud(request, PdpaSettingsForm, PdpaSettings, pk, _("DPO and deadlines"),
                 "orvahr_pdpa.add_pdpasettings", "orvahr_pdpa.change_pdpasettings")


@login_required
def notice_form(request, pk=None):
    return _crud(request, PrivacyNoticeForm, PrivacyNotice, pk, _("Privacy notice"),
                 "orvahr_pdpa.add_privacynotice", "orvahr_pdpa.change_privacynotice")


@login_required
def purpose_form(request, pk=None):
    return _crud(request, ConsentPurposeForm, ConsentPurpose, pk, _("Consent purpose"),
                 "orvahr_pdpa.add_consentpurpose", "orvahr_pdpa.change_consentpurpose")


@login_required
def policy_form(request, pk=None):
    return _crud(request, RetentionPolicyForm, RetentionPolicy, pk, _("Retention policy"),
                 "orvahr_pdpa.add_retentionpolicy", "orvahr_pdpa.change_retentionpolicy")


@login_required
@permission_required("orvahr_pdpa.view_privacynotice")
def notice_detail(request, pk):
    notice = get_object_or_404(PrivacyNotice.objects, pk=pk)
    return render(request, "orvahr_pdpa/notice_detail.html", {"notice": notice})


# Employee self-service -------------------------------------------------------


@login_required
def my_privacy(request):
    employee = getattr(request.user, "employee_get", None)
    if employee is None:
        raise Http404
    notice = consent_service.current_notice_for_employee(employee)
    purposes = list(consent_service.purposes_for_employee(employee))
    # Set by the login redirect: where the employee was going. Local URLs only.
    next_url = request.POST.get("next") or request.GET.get("next") or ""
    if not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        next_url = ""
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "acknowledge" and notice is not None:
            consent_service.acknowledge_notice(employee, notice, request=request)
            messages.success(request, _("Thank you. Your acknowledgement was recorded."))
            if next_url:
                return redirect(next_url)
        elif action in ("grant", "withdraw"):
            purpose = next((p for p in purposes if str(p.pk) == request.POST.get("purpose")), None)
            if purpose is None or not purpose.optional:
                messages.error(request, _("This consent cannot be changed here."))
            else:
                consent_service.set_employee_consent(employee, purpose, action == "grant", request=request)
                messages.success(request, _("Your consent choice was saved."))
        elif action == "request":
            request_type = request.POST.get("request_type")
            if request_type in dict(DataSubjectRequest.TYPE_CHOICES):
                dsr = DataSubjectRequest(
                    employee=employee,
                    company=employee_company(employee),
                    request_type=request_type,
                    details=(request.POST.get("details") or "")[:5000],
                )
                dsr.save()
                notify_dsr_event(dsr, "created", actor=employee)
                messages.success(request, _("Your request was sent to the data protection officer."))
            else:
                messages.error(request, _("Choose a request type."))
        return redirect("orvahr_pdpa:my-privacy")

    states = consent_service.employee_states(employee)
    rows = [{"purpose": p, "record": states.get(p.pk)} for p in purposes]
    return render(
        request,
        "orvahr_pdpa/my_privacy.html",
        {
            "employee": employee,
            "notice": notice,
            "acknowledged": consent_service.has_acknowledged(employee, notice),
            "next_url": next_url,
            "rows": rows,
            "history": ConsentRecord.all_objects.filter(employee=employee).select_related("purpose")[:50],
            "my_requests": DataSubjectRequest.all_objects.filter(employee=employee).order_by("-received_at")[:20],
            "request_types": DataSubjectRequest.TYPE_CHOICES,
            "dpo": PdpaSettings.for_company(employee_company(employee)),
        },
    )


# HR consent status -----------------------------------------------------------


@login_required
@permission_required("orvahr_pdpa.view_consentrecord")
def consent_status(request):
    employees = Employee.objects.filter(is_active=True).select_related("employee_work_info__company_id")
    search = (request.GET.get("search") or "").strip()
    if search:
        employees = employees.filter(
            Q(employee_first_name__icontains=search) | Q(employee_last_name__icontains=search)
            | Q(badge_id__icontains=search)
        )
    page = _page(request, employees.order_by("employee_first_name", "id"))
    ids = [e.pk for e in page]
    records = (
        ConsentRecord.all_objects.filter(employee_id__in=ids)
        .select_related("purpose", "notice")
        .order_by("-created_at", "-id")
    )
    per_employee = {}
    for record in records:
        per_employee.setdefault(record.employee_id, []).append(record)
    purposes = list(ConsentPurpose.objects.filter(audience="employee", is_active=True))
    rows = []
    for employee in page:
        company = employee_company(employee)
        notice = PrivacyNotice.current("employee", company)
        states = consent_service.latest_states(per_employee.get(employee.pk, []))
        acknowledged = notice is not None and any(
            r.notice_id == notice.pk and r.action == "acknowledged" and r.purpose_id is None
            for r in per_employee.get(employee.pk, [])
        )
        rows.append(
            {
                "employee": employee,
                "notice": notice,
                "acknowledged": acknowledged,
                "purposes": [states.get(p.pk) for p in purposes],
            }
        )
    return render(
        request,
        "orvahr_pdpa/consent_status.html",
        {"rows": rows, "page": page, "purposes": purposes, "search": search, "query": _query_without_page(request)},
    )


# Data subject requests ---------------------------------------------------


@login_required
@permission_required("orvahr_pdpa.view_datasubjectrequest")
def dsr_list(request):
    view = request.GET.get("view", "open")
    qs = DataSubjectRequest.objects.select_related("employee", "candidate", "assignee")
    if view == "overdue":
        qs = qs.filter(status__in=DataSubjectRequest.OPEN_STATUSES, due_at__lt=timezone.now())
    elif view == "open":
        qs = qs.filter(status__in=DataSubjectRequest.OPEN_STATUSES)
    return render(
        request,
        "orvahr_pdpa/dsr_list.html",
        {"page": _page(request, qs.order_by("due_at", "id")), "view": view, "query": _query_without_page(request)},
    )


@login_required
@permission_required("orvahr_pdpa.add_datasubjectrequest")
def dsr_create(request):
    form = DataSubjectRequestForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        dsr = form.save()
        notify_dsr_event(dsr, "created", actor=getattr(request.user, "employee_get", None))
        messages.success(request, _("Request recorded."))
        return redirect("orvahr_pdpa:dsr-detail", pk=dsr.pk)
    return _form_page(request, form, _("New data subject request"), reverse("orvahr_pdpa:dsr-list"))


@login_required
@permission_required("orvahr_pdpa.view_datasubjectrequest")
def dsr_detail(request, pk):
    dsr = get_object_or_404(DataSubjectRequest.objects.select_related("employee", "candidate", "assignee"), pk=pk)
    form = None
    if request.user.has_perm("orvahr_pdpa.change_datasubjectrequest"):
        old_status, old_assignee = dsr.status, dsr.assignee_id
        form = DsrUpdateForm(request.POST or None, instance=dsr)
        if request.method == "POST" and form.is_valid():
            dsr = form.save()
            actor = getattr(request.user, "employee_get", None)
            if dsr.status != old_status:
                notify_dsr_event(dsr, "status", actor=actor)
            if dsr.assignee_id and dsr.assignee_id != old_assignee:
                notify_dsr_event(dsr, "assigned", actor=actor)
            messages.success(request, _("Request updated."))
            return redirect("orvahr_pdpa:dsr-detail", pk=dsr.pk)
    elif request.method == "POST":
        return HttpResponseNotAllowed(["GET"])
    return render(
        request,
        "orvahr_pdpa/dsr_detail.html",
        {
            "dsr": dsr,
            "form": form,
            "can_export": request.user.has_perm("orvahr_pdpa.export_personal_data") and dsr.employee_id,
        },
    )


# Export ------------------------------------------------------------------


@login_required
@permission_required("orvahr_pdpa.export_personal_data")
@require_POST
def export_employee(request, employee_id):
    # Company-scoped manager: an officer cannot export outside their company.
    employee = get_object_or_404(Employee.objects, pk=employee_id)
    spool, errors = build_zip(employee)
    dsr_id = request.POST.get("dsr") or ""
    log_sensitive_access(
        request,
        employee,
        ["export:personal_data"],
        context=f"pdpa_export dsr={dsr_id}" if dsr_id.isdigit() else "pdpa_export",
    )
    filename = f"personal-data-{employee.pk}-{timezone.localdate():%Y%m%d}.zip"
    response = FileResponse(spool, as_attachment=True, filename=filename, content_type="application/zip")
    response["Cache-Control"] = "no-store, private"
    response["X-Content-Type-Options"] = "nosniff"
    if errors:
        response["X-Export-Incomplete"] = ",".join(errors)
    return response


# Retention ---------------------------------------------------------------


@login_required
@permission_required("orvahr_pdpa.view_retentionpolicy")
def retention_report(request):
    rows = retention.dry_run_report(RetentionPolicy.objects.filter(is_active=True), sample=10)
    return render(request, "orvahr_pdpa/retention_report.html", {"rows": rows})


@login_required
@permission_required("orvahr_pdpa.view_retentionpolicy")
def retention_policy_detail(request, pk):
    policy = get_object_or_404(RetentionPolicy.objects, pk=pk)
    error, records, total = "", [], 0
    try:
        qs = retention.expired_queryset(policy)
        total = qs.count()
        records = list(qs[:200])
    except Exception as exc:
        error = str(getattr(exc, "message_dict", exc))
    can_anonymise = policy.can_anonymise and request.user.is_superuser and not error
    return render(
        request,
        "orvahr_pdpa/retention_policy.html",
        {
            "policy": policy,
            "records": records,
            "total": total,
            "error": error,
            "cutoff": retention.cutoff_for(policy),
            "can_anonymise": can_anonymise,
            "form": AnonymiseConfirmForm() if can_anonymise else None,
        },
    )


@login_required
@superuser_required
@require_POST
def retention_anonymise(request, pk):
    policy = get_object_or_404(RetentionPolicy.objects, pk=pk)
    if not policy.can_anonymise or not policy.is_active:
        messages.error(request, _("This policy does not allow anonymisation."))
        return redirect("orvahr_pdpa:retention-policy", pk=pk)
    form = AnonymiseConfirmForm(request.POST)
    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
        return redirect("orvahr_pdpa:retention-policy", pk=pk)
    eligible = retention.expired_queryset(policy).filter(pk__in=form.cleaned_data["record_ids"])
    done = 0
    for employee in eligible:
        log_sensitive_access(request, employee, ["anonymise:employee"], context=f"pdpa_retention policy={policy.pk}")
        retention.anonymise_employee(employee)
        done += 1
    skipped = len(set(form.cleaned_data["record_ids"])) - done
    messages.success(request, _("%(done)s record(s) anonymised.") % {"done": done})
    if skipped:
        messages.warning(request, _("%(n)s record(s) skipped: not past retention.") % {"n": skipped})
    return redirect("orvahr_pdpa:retention-policy", pk=pk)


# Sensitive access log ------------------------------------------------------


def _day_bound(value, end=False):
    day = parse_date(value or "")
    if day is None:
        return None
    return timezone.make_aware(datetime.combine(day, time.max if end else time.min))


@login_required
@permission_required("orvahr_pdpa.view_sensitiveaccesslog")
def access_log(request):
    qs = SensitiveAccessLog.objects.select_related("subject_employee")
    user_q = (request.GET.get("user") or "").strip()
    subject_q = (request.GET.get("subject") or "").strip()
    field_q = (request.GET.get("field") or "").strip()
    start, end = _day_bound(request.GET.get("from")), _day_bound(request.GET.get("to"), end=True)
    if user_q:
        qs = qs.filter(username__icontains=user_q)
    if subject_q:
        qs = qs.filter(subject_label__icontains=subject_q)
    if field_q:
        # JSON contains is not portable to SQLite; the list is short text.
        qs = qs.filter(fields__icontains=field_q)
    if start:
        qs = qs.filter(accessed_at__gte=start)
    if end:
        qs = qs.filter(accessed_at__lte=end)
    return render(
        request,
        "orvahr_pdpa/access_log.html",
        {"page": _page(request, qs), "filters": request.GET, "query": _query_without_page(request)},
    )


# Breaches ----------------------------------------------------------------


@login_required
@permission_required("orvahr_pdpa.view_breachincident")
def breach_list(request):
    view = request.GET.get("view", "open")
    qs = BreachIncident.objects.select_related("company")
    if view == "open":
        qs = qs.exclude(status="closed")
    return render(
        request,
        "orvahr_pdpa/breach_list.html",
        {"page": _page(request, qs), "view": view, "query": _query_without_page(request), "now": timezone.now()},
    )


@login_required
def breach_form(request, pk=None):
    return _crud(request, BreachIncidentForm, BreachIncident, pk, _("Breach incident"),
                 "orvahr_pdpa.add_breachincident", "orvahr_pdpa.change_breachincident",
                 back="orvahr_pdpa:breach-list")
