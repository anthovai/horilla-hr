"""
Face check-in pages.

The browser captures frames with getUserMedia and posts them as base64 in a
JSON body to the ``api/`` endpoints here; Django calls services/face-verify.
The browser never talks to the face service.
"""

import json
from datetime import timedelta

from django.contrib import messages
from django.core.exceptions import RequestDataTooBig
from django.db.models import Count, Max, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from employee.models import Employee
from horilla.decorators import login_required, permission_required
from orvahr_face import consent, services
from orvahr_face.forms import FaceSettingsForm
from orvahr_face.models import MAX_ACTIVE_TEMPLATES, FaceAttempt, FaceSettings, FaceTemplate

STATS_DAYS = 30


def _employee(request):
    employee = getattr(request.user, "employee_get", None)
    if employee is None:
        raise Http404
    return employee


def _json_body(request):
    try:
        data = json.loads(request.body or b"{}")
    except RequestDataTooBig:
        return None, JsonResponse(
            {"ok": False, "code": "invalid_image", "message": _("The pictures are too large.")}, status=413
        )
    except (ValueError, UnicodeDecodeError):
        return None, JsonResponse({"ok": False, "code": "invalid_image", "message": _("Invalid request.")}, status=400)
    if not isinstance(data, dict):
        return None, JsonResponse({"ok": False, "code": "invalid_image", "message": _("Invalid request.")}, status=400)
    return data, None


def _status(employee):
    row = services.settings_for_employee(employee)
    templates = services.templates_qs(employee)
    return {
        "settings": row,
        "enabled": bool(row and row.enabled),
        "pdpa": consent.pdpa_available(),
        "purpose": consent.purpose_for(employee),
        "consented": consent.has_consent(employee),
        "consent_record": consent.latest_record(employee),
        "template_count": templates.count(),
        "last_enrolled": templates.aggregate(m=Max("created_at"))["m"],
        "max_templates": MAX_ACTIVE_TEMPLATES,
    }


def _capture_strings():
    """UI strings for face_capture.js (escaped with |escapejs in the template)."""
    return {
        "starting": _("Starting the camera..."),
        "no_camera": _("Camera not available. Allow camera access in your browser settings, or use the normal check-in."),
        "insecure": _("The camera needs a secure (https) connection."),
        "guide_center": _("Put your face inside the oval, in good light."),
        "guide_turn_left": _("Turn your head slightly to the left."),
        "guide_turn_right": _("Turn your head slightly to the right."),
        "guide_hold": _("Hold still..."),
        "captured": _("Picture %(n)s of %(total)s taken."),
        "sending": _("Checking..."),
        "network": _("Network error. Try again."),
        "retry": _("Try again"),
        "frame": _("Picture"),
        "accepted": _("OK"),
    }


# Employee self-service ----------------------------------------------------


@login_required
def my_face(request):
    employee = _employee(request)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "grant":
            if request.POST.get("agree") != "yes":
                messages.error(request, _("Tick the box to confirm you agree."))
            elif consent.grant(employee, request=request):
                messages.success(request, _("Thank you. Your biometric consent was recorded."))
            else:
                messages.error(request, _("Biometric consent cannot be recorded. Ask HR to check the PDPA settings."))
        elif action == "withdraw":
            consent.withdraw(employee, request=request)
            messages.success(request, _("Consent withdrawn. Your face templates were deleted."))
        elif action == "delete":
            services.delete_templates(employee, "self", request=request)
            messages.success(request, _("Your face templates were deleted."))
        return redirect("orvahr_face:my-face")
    context = _status(employee)
    context.update({"employee": employee, "purpose_text": consent.PURPOSE_TEXT})
    return render(request, "orvahr_face/my_face.html", context)


@login_required
def enrol_page(request, employee_id=None):
    if employee_id is None:
        employee, assisted = _employee(request), False
    else:
        if not request.user.has_perm("orvahr_face.enrol_others_face"):
            return render(request, "403.html", status=403)
        employee, assisted = get_object_or_404(Employee.objects.all(), pk=employee_id), True
    if assisted and request.method == "POST" and request.POST.get("action") == "hr_consent":
        if request.POST.get("agree") != "yes":
            messages.error(request, _("Confirm that the employee read the text and agreed in person."))
        elif consent.grant(employee, request=request, channel="hr"):
            messages.success(request, _("The employee's biometric consent was recorded."))
        return redirect("orvahr_face:enrol-employee", employee_id=employee.pk)
    context = _status(employee)
    context.update(
        {
            "employee": employee,
            "assisted": assisted,
            "purpose_text": consent.PURPOSE_TEXT,
            "strings": _capture_strings(),
            "frames_wanted": 5,
        }
    )
    return render(request, "orvahr_face/enrol.html", context)


@login_required
@require_POST
def enrol_api(request):
    data, error = _json_body(request)
    if error:
        return error
    employee_id = data.get("employee_id")
    own = _employee(request)
    if employee_id in (None, "", own.pk, str(own.pk)):
        employee, source = own, "self"
    else:
        if not request.user.has_perm("orvahr_face.enrol_others_face"):
            return JsonResponse({"ok": False, "code": "forbidden", "message": _("Permission denied.")}, status=403)
        try:
            employee = Employee.objects.filter(pk=int(employee_id)).first()
        except (TypeError, ValueError):
            employee = None
        if employee is None:
            return JsonResponse({"ok": False, "code": "not_found", "message": _("Employee not found.")}, status=404)
        source = "hr"
    outcome = services.enrol(employee, data.get("images"), request=request, source=source)
    return JsonResponse(outcome.as_json(), status=outcome.status if not outcome.ok else 200)


@login_required
def check_in_page(request):
    employee = _employee(request)
    context = _status(employee)
    context.update(
        {
            "employee": employee,
            "enrolled": services.is_enrolled(employee),
            "direction": services.current_direction(employee),
            "strings": _capture_strings(),
        }
    )
    return render(request, "orvahr_face/check_in.html", context)


@login_required
@require_POST
def check_in_api(request):
    employee = _employee(request)
    data, error = _json_body(request)
    if error:
        return error
    outcome = services.face_check(request, employee, data.get("image"), str(data.get("direction") or ""))
    return JsonResponse(outcome.as_json(), status=200 if outcome.ok else outcome.status)


# HR -------------------------------------------------------------------------


@login_required
@permission_required("orvahr_face.view_facetemplate")
def enrolments(request):
    """Who is enrolled (no biometric data), and failure statistics."""
    since = timezone.now() - timedelta(days=STATS_DAYS)
    rows = (
        FaceTemplate.objects.values("employee")
        .annotate(count=Count("id"), last=Max("created_at"))
        .order_by("-last")
    )
    employees = Employee.objects.filter(pk__in=[r["employee"] for r in rows]).select_related("employee_work_info")
    by_id = {e.pk: e for e in employees}
    failures = dict(
        FaceAttempt.objects.filter(kind="verify", created_at__gte=since)
        .exclude(outcome="success")
        .values_list("employee")
        .annotate(n=Count("id"))
    )
    enrolled = []
    for row in rows:
        employee = by_id.get(row["employee"])
        if employee is None:
            continue
        enrolled.append(
            {
                "employee": employee,
                "count": row["count"],
                "last": row["last"],
                "consented": consent.has_consent(employee),
                "failures": failures.get(employee.pk, 0),
            }
        )
    stats = (
        FaceAttempt.objects.filter(kind="verify", created_at__gte=since)
        .values("outcome")
        .annotate(n=Count("id"))
        .order_by("-n")
    )
    outcome_names = dict(FaceAttempt.OUTCOME_CHOICES)
    can_enrol = request.user.has_perm("orvahr_face.enrol_others_face")
    totals = FaceAttempt.objects.filter(kind="verify", created_at__gte=since).aggregate(
        total=Count("id"), ok=Count("id", filter=Q(outcome="success"))
    )
    return render(
        request,
        "orvahr_face/enrolments.html",
        {
            "enrolled": enrolled,
            "stats": [{"outcome": outcome_names.get(s["outcome"], s["outcome"]), "n": s["n"]} for s in stats],
            "totals": totals,
            "days": STATS_DAYS,
            "can_enrol": can_enrol,
            "candidates": (
                Employee.objects.filter(is_active=True).order_by("employee_first_name", "employee_last_name")[:2000]
                if can_enrol
                else []
            ),
            "can_delete": request.user.has_perm("orvahr_face.delete_facetemplate"),
        },
    )


@login_required
@require_POST
@permission_required("orvahr_face.delete_facetemplate")
def enrolment_delete(request, employee_id):
    employee = get_object_or_404(Employee.objects.all(), pk=employee_id)
    count = services.delete_templates(employee, "hr", request=request)
    messages.success(request, _("%(count)s face template(s) deleted.") % {"count": count})
    return redirect("orvahr_face:enrolments")


def _selected_company(request):
    from base.models import Company

    selected = request.session.get("selected_company")
    if not selected or selected == "all":
        return None
    return Company.objects.filter(pk=selected).first()


@login_required
@permission_required("orvahr_face.view_facesettings")
def face_settings(request):
    company = _selected_company(request)
    instance = FaceSettings.all_objects.filter(company=company).first()
    can_change = request.user.has_perm("orvahr_face.change_facesettings") and (
        instance is not None or request.user.has_perm("orvahr_face.add_facesettings")
    )
    form = FaceSettingsForm(request.POST or None, instance=instance)
    if request.method == "POST":
        if not can_change:
            return render(request, "403.html", status=403)
        if form.is_valid():
            row = form.save(commit=False)
            row.company = company
            row.save()
            messages.success(request, _("Face check-in settings saved."))
            return redirect("orvahr_face:settings")
    return render(
        request,
        "orvahr_face/settings.html",
        {
            "form": form,
            "company": company,
            "can_change": can_change,
            "inherited": instance is None and FaceSettings.for_company(company) is not None,
            "legacy_photos": _legacy_photo_count(),
        },
    )


def _legacy_photo_count():
    from django.apps import apps

    if not apps.is_installed("facedetection"):
        return 0
    from facedetection.models import EmployeeFaceDetection

    return EmployeeFaceDetection.objects.count()
