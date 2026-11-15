"""
Face check-in logic: enrolment, verification, the attendance punch, deletion
and retention. Views stay thin and call these; nothing here raises into a
view for a service failure.

Images
------
Frames arrive as base64 strings inside a JSON request body (never a
multipart upload, so Django's upload handlers never write a temp file), are
checked for size and JPEG/PNG magic bytes, forwarded to the face service and
dropped with the request. Nothing touches MEDIA_ROOT or the temp directory.
"""

import base64
import binascii
import inspect
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.contrib.messages import constants as message_levels
from django.contrib.messages import get_messages
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.translation import gettext as _

from horilla.client_ip import client_ip
from orvahr_face import client as face_client
from orvahr_face import consent
from orvahr_face.crypto import decrypt_template, encrypt_template
from orvahr_face.models import MAX_ACTIVE_TEMPLATES, FaceAttempt, FaceSettings, FaceTemplate

logger = logging.getLogger(__name__)

MIN_ENROL_FRAMES = 1
MAX_ENROL_FRAMES = 5
MAX_IMAGE_B64_CHARS = 1_400_000  # about 1 MB decoded per frame
MIN_IMAGE_BYTES = 512
FIELDS_TEMPLATE = ["face_template"]
FIELDS_VERIFY = ["face_template", "face_image_transient"]

# Outcomes that count towards the verification rate limit.
RATE_LIMITED_OUTCOMES = (
    "no_match",
    "not_live",
    "no_face",
    "multiple_faces",
    "too_small",
    "blurry",
    "invalid_image",
)


def rate_limit():
    """(attempts, window seconds) -- override with ORVAHR_FACE_RATE_LIMIT = (n, seconds)."""
    value = getattr(settings, "ORVAHR_FACE_RATE_LIMIT", None) or (5, 300)
    return int(value[0]), int(value[1])


def reason_text(code):
    """Message shown to the employee for a service reason / outcome code."""
    texts = {
        "no_face": _("No face found. Face the camera in good light."),
        "multiple_faces": _("More than one face in the picture. Only you should be in the frame."),
        "too_small": _("Your face is too small. Move closer to the camera."),
        "blurry": _("The picture is blurry. Hold still and try again."),
        "not_live": _("Liveness check failed. Use your real face, not a photo or a screen."),
        "model_mismatch": _("Your face enrolment is out of date. Please enrol again."),
        "no_match": _("Your face did not match your enrolment. Try again."),
        "not_enrolled": _("You have not enrolled your face yet."),
        "no_consent": _("Biometric consent is needed before using face check-in."),
        "rate_limited": _("Too many attempts. Wait a few minutes, or use the normal check-in."),
        "invalid_image": _("The camera picture could not be read. Try again."),
        "service_unavailable": _("The face service is unavailable right now. Use the normal check-in or try again later."),
        "disabled": _("Face check-in is not enabled for your company."),
        "no_pdpa": _("Face check-in needs the PDPA module to record consent."),
        "state_changed": _("Your check-in status changed. Reload the page and try again."),
        "no_work_info": _("Your employee profile or work information is incomplete."),
    }
    return texts.get(code) or _("Face check failed. Try again.")


# Settings ----------------------------------------------------------------


def employee_company(employee):
    work_info = getattr(employee, "employee_work_info", None) if employee else None
    return getattr(work_info, "company_id", None) if work_info else None


def settings_for(company):
    return FaceSettings.for_company(company)


def settings_for_employee(employee):
    return settings_for(employee_company(employee))


def face_enabled(company):
    row = settings_for(company)
    return bool(row and row.enabled)


# Images --------------------------------------------------------------------


class InvalidImage(ValueError):
    pass


def clean_image(value):
    """Base64 body of a JPEG/PNG frame (data-URL prefix stripped). Raises InvalidImage."""
    if not isinstance(value, str) or not value:
        raise InvalidImage("empty")
    if value.startswith("data:"):
        header, _sep, value = value.partition(",")
        if ";base64" not in header or not header[5:].startswith(("image/jpeg", "image/png")):
            raise InvalidImage("type")
    value = value.strip()
    if len(value) > MAX_IMAGE_B64_CHARS:
        raise InvalidImage("size")
    try:
        head = base64.b64decode(value[:64], validate=True)
        size = len(base64.b64decode(value, validate=True))
    except (binascii.Error, ValueError):
        raise InvalidImage("encoding")
    if size < MIN_IMAGE_BYTES:
        raise InvalidImage("size")
    if not (head.startswith(b"\xff\xd8\xff") or head.startswith(b"\x89PNG\r\n\x1a\n")):
        raise InvalidImage("type")
    return value


# Logging -------------------------------------------------------------------


def _ip(request):
    if request is None:
        return None
    try:
        return client_ip(request) or None
    except Exception:
        return None


def _actor(request):
    user = getattr(request, "user", None)
    return user if user is not None and user.is_authenticated else None


def record_attempt(employee, kind, outcome, *, request=None, direction="", detail="", score=None, activity=None):
    try:
        return FaceAttempt.all_objects.create(
            company=employee_company(employee),
            employee=employee,
            kind=kind,
            direction=direction,
            outcome=outcome,
            detail=str(detail or "")[:100],
            score=round(float(score), 4) if isinstance(score, (int, float)) else None,
            attendance_activity=activity,
            actor=_actor(request),
            ip_address=_ip(request),
        )
    except Exception:
        logger.exception("Face attempt could not be recorded")
        return None


# Templates -----------------------------------------------------------------


def retention_cutoff(employee):
    row = settings_for_employee(employee)
    days = row.retention_days if row else None
    if not days:
        from orvahr_face.models import DEFAULT_RETENTION_DAYS

        days = DEFAULT_RETENTION_DAYS
    return timezone.now() - timedelta(days=days)


def templates_qs(employee):
    return FaceTemplate.all_objects.filter(employee=employee)


def is_enrolled(employee):
    return templates_qs(employee).filter(created_at__gte=retention_cutoff(employee)).exists()


def delete_templates(employee, reason, *, request=None):
    """Delete every template of ``employee`` now. Logged even when there was nothing."""
    with transaction.atomic():
        count, _detail = templates_qs(employee).delete()
    if count:
        record_attempt(employee, "delete", "deleted", request=request, detail=f"{reason}:{count}")
        consent.log_access(request, employee, FIELDS_TEMPLATE, f"face_delete:{reason}")
    return count


def _delete_expired(employee, request=None):
    expired = templates_qs(employee).filter(created_at__lt=retention_cutoff(employee))
    count, _detail = expired.delete()
    if count:
        record_attempt(employee, "delete", "deleted", request=request, detail=f"retention:{count}")
        consent.log_access(request, employee, FIELDS_TEMPLATE, "face_delete:retention")
    return count


def _split_model(model):
    model = str(model or "")[:150]
    if "@" in model:
        model_id, _sep, version = model.rpartition("@")
        return model_id[:100], version[:50]
    return model[:100], ""


def usable_templates(employee):
    """(full model id, [vectors]) of the newest model the employee enrolled with."""
    _delete_expired(employee)
    rows = list(templates_qs(employee).order_by("-created_at", "-id")[:MAX_ACTIVE_TEMPLATES])
    if not rows:
        return "", []
    model = rows[0].full_model
    vectors = []
    for row in rows:
        if row.full_model != model:
            continue
        vector = decrypt_template(row.template_encrypted, employee.pk)
        if vector:
            vectors.append(vector)
    return model, vectors


# Enrolment ----------------------------------------------------------------


@dataclass
class Outcome:
    ok: bool
    code: str = ""
    message: str = ""
    frames: list = field(default_factory=list)
    score: float = None
    direction: str = ""
    status: int = 200

    def as_json(self):
        data = {"ok": self.ok, "code": self.code, "message": str(self.message)}
        if self.frames:
            data["frames"] = self.frames
        if self.direction:
            data["direction"] = self.direction
        return data


def _fail(code, status=400, **kwargs):
    return Outcome(ok=False, code=code, message=reason_text(code), status=status, **kwargs)


def _frames_report(frames):
    report = []
    for index, frame in enumerate(frames or []):
        if not isinstance(frame, dict):
            continue
        reason = frame.get("reason")
        report.append(
            {
                "index": index,
                "accepted": bool(frame.get("accepted")),
                "reason": reason or "",
                "message": str(reason_text(reason)) if reason else "",
            }
        )
    return report


def _quality_summary(frames):
    accepted = [f for f in frames or [] if isinstance(f, dict) and f.get("accepted")]

    def mean(key):
        values = [float(f[key]) for f in accepted if isinstance(f.get(key), (int, float))]
        return round(sum(values) / len(values), 3) if values else None

    rejected = {}
    for f in frames or []:
        if isinstance(f, dict) and not f.get("accepted") and f.get("reason"):
            rejected[str(f["reason"])[:30]] = rejected.get(str(f["reason"])[:30], 0) + 1
    return {
        "frames_sent": len(frames or []),
        "frames_accepted": len(accepted),
        "mean_quality": mean("quality"),
        "mean_live_score": mean("live_score"),
        "rejected": rejected,
    }


def enrol(employee, images, *, request=None, source="self"):
    """Create one template from 1..5 frames. Returns an Outcome."""
    row = settings_for_employee(employee)
    if not (row and row.enabled):
        return _fail("disabled", status=403)
    if not consent.pdpa_available():
        return _fail("no_pdpa", status=403)
    if not consent.has_consent(employee):
        record_attempt(employee, "enrol", "no_consent", request=request)
        return _fail("no_consent", status=403)
    if not isinstance(images, list) or not (MIN_ENROL_FRAMES <= len(images) <= MAX_ENROL_FRAMES):
        return _fail("invalid_image")
    try:
        cleaned = [clean_image(image) for image in images]
    except InvalidImage as exc:
        record_attempt(employee, "enrol", "invalid_image", request=request, detail=str(exc))
        return _fail("invalid_image")

    result = face_client.get_client().embed(cleaned)
    del cleaned
    if result.error == face_client.REJECTED:
        frames = _frames_report(result.data.get("frames"))
        reasons = sorted({f["reason"] for f in frames if f["reason"]})
        code = reasons[0] if len(reasons) == 1 else "no_face"
        record_attempt(employee, "enrol", code, request=request, detail=",".join(reasons))
        outcome = _fail(code, status=422, frames=frames)
        outcome.message = _("No picture was good enough. Check the reasons below and try again.")
        return outcome
    if not result.ok:
        record_attempt(employee, "enrol", "service_unavailable", request=request, detail=result.error)
        return _fail("service_unavailable", status=503)

    data = result.data
    vector = data.get("template")
    frames = data.get("frames") or []
    try:
        if not isinstance(vector, list) or not vector:
            raise ValueError("no template")
        if row.liveness_required and any(
            isinstance(f, dict) and f.get("accepted") and f.get("reason") == "not_live" for f in frames
        ):
            raise ValueError("liveness")
        token = encrypt_template(vector, employee.pk)
    except (TypeError, ValueError):
        record_attempt(employee, "enrol", "service_unavailable", request=request, detail="bad_template")
        return _fail("service_unavailable", status=503)
    del vector

    model_id, model_version = _split_model(data.get("model"))
    summary = _quality_summary(frames)
    with transaction.atomic():
        FaceTemplate.all_objects.create(
            company=employee_company(employee),
            employee=employee,
            template_encrypted=token,
            model_id=model_id,
            model_version=model_version,
            dims=int(data.get("dims") or 0),
            frames_count=summary["frames_accepted"],
            quality_summary=summary,
            source=source if source in ("self", "hr") else "self",
            created_by=_actor(request),
        )
        keep = list(
            templates_qs(employee).order_by("-created_at", "-id").values_list("pk", flat=True)[:MAX_ACTIVE_TEMPLATES]
        )
        templates_qs(employee).exclude(pk__in=keep).delete()
    record_attempt(employee, "enrol", "success", request=request, detail=source)
    consent.log_access(request, employee, FIELDS_TEMPLATE, f"face_enrol:{source}")
    return Outcome(
        ok=True,
        code="success",
        message=_("Your face is enrolled. You can now use face check-in."),
        frames=_frames_report(frames),
    )


# Verification ---------------------------------------------------------------


def is_rate_limited(employee):
    attempts, window = rate_limit()
    since = timezone.now() - timedelta(seconds=window)
    recent = FaceAttempt.all_objects.filter(
        employee=employee, kind="verify", created_at__gte=since, outcome__in=RATE_LIMITED_OUTCOMES
    ).count()
    return recent >= attempts


def verify(employee, image, *, request=None, direction=""):
    """Check ``image`` against the employee's templates. Outcome.ok means matched and live."""
    row = settings_for_employee(employee)
    if not (row and row.enabled):
        return _fail("disabled", status=403)
    if not consent.has_consent(employee):
        record_attempt(employee, "verify", "no_consent", request=request, direction=direction)
        return _fail("no_consent", status=403)
    if is_rate_limited(employee):
        record_attempt(employee, "verify", "rate_limited", request=request, direction=direction)
        return _fail("rate_limited", status=429)
    try:
        cleaned = clean_image(image)
    except InvalidImage as exc:
        record_attempt(employee, "verify", "invalid_image", request=request, direction=direction, detail=str(exc))
        return _fail("invalid_image")
    model, vectors = usable_templates(employee)
    if not vectors:
        record_attempt(employee, "verify", "not_enrolled", request=request, direction=direction)
        return _fail("not_enrolled", status=409)

    result = face_client.get_client().verify(cleaned, vectors, model)
    del cleaned, vectors
    consent.log_access(request, employee, FIELDS_VERIFY, "face_verify")
    if not result.ok:
        record_attempt(
            employee, "verify", "service_unavailable", request=request, direction=direction, detail=result.error
        )
        return _fail("service_unavailable", status=503)

    data = result.data
    score = data.get("score") if isinstance(data.get("score"), (int, float)) else None
    service_threshold = data.get("threshold") if isinstance(data.get("threshold"), (int, float)) else None
    threshold = row.match_threshold if row.match_threshold is not None else service_threshold
    faces = data.get("faces")
    reason = data.get("reason") or ""
    live = bool(data.get("live"))

    if reason == "model_mismatch":
        code = "model_mismatch"
    elif faces == 0 or reason == "no_face":
        code = "no_face"
    elif (faces is not None and faces != 1) or reason == "multiple_faces":
        code = "multiple_faces"
    elif reason in ("too_small", "blurry"):
        code = reason
    elif row.liveness_required and (not live or reason == "not_live"):
        code = "not_live"
    elif score is None or threshold is None or score < threshold:
        code = "no_match"
    elif row.match_threshold is None and row.liveness_required and not data.get("match"):
        # Service default threshold and liveness: trust the service's own verdict too.
        code = "no_match"
    else:
        code = "success"
    if code != "success":
        record_attempt(employee, "verify", code, request=request, direction=direction, score=score)
        return _fail(code, status=403, score=score)
    return Outcome(ok=True, code="success", score=score, direction=direction)


# Attendance punch -----------------------------------------------------------


def _activities(employee):
    from attendance.models import AttendanceActivity

    return AttendanceActivity._base_manager.filter(employee_id=employee)


def current_direction(employee):
    """'out' when the employee has an open check-in, else 'in'."""
    return "out" if _activities(employee).filter(clock_out__isnull=True).exists() else "in"


def _drain_errors(request):
    errors = []
    try:
        for message in get_messages(request):
            if message.level >= message_levels.WARNING:
                errors.append(str(message))
    except Exception:
        pass
    return errors


def punch(request, employee, direction):
    """
    Clock in/out through the attendance app's own views, so shift, grace
    time, late-come/early-out, IP and geo-fence rules apply unchanged. Returns
    (activity or None, [error messages]).
    """
    from attendance.views.clock_in_out import clock_in, clock_out

    activities = _activities(employee)
    before_max = activities.aggregate(m=Max("id"))["m"] or 0
    open_before = activities.filter(clock_out__isnull=True).order_by("-id").first()
    view = inspect.unwrap(clock_in if direction == "in" else clock_out)
    request.orvahr_face_verified = True
    try:
        view(request)
    except Exception:
        logger.exception("Face check-in: attendance %s view failed", direction)
        return None, [str(_("Attendance could not be recorded. Use the normal check-in."))]
    finally:
        request.orvahr_face_verified = False
    errors = _drain_errors(request)
    if direction == "in":
        activity = activities.filter(id__gt=before_max).order_by("-id").first()
    else:
        activity = None
        if open_before is not None:
            activity = activities.filter(pk=open_before.pk, clock_out__isnull=False).first()
    return activity, errors


def face_check(request, employee, image, direction):
    """Verify, then punch. Returns an Outcome."""
    if direction not in ("in", "out"):
        return _fail("state_changed", status=409)
    if getattr(employee, "employee_work_info", None) is None:
        return _fail("no_work_info", status=409)
    if current_direction(employee) != direction:
        return _fail("state_changed", status=409)
    outcome = verify(employee, image, request=request, direction=direction)
    if not outcome.ok:
        outcome.direction = direction
        return outcome
    activity, errors = punch(request, employee, direction)
    if activity is None:
        record_attempt(
            employee, "verify", "attendance_refused", request=request, direction=direction,
            score=outcome.score, detail="; ".join(errors)
        )
        message = errors[0] if errors else _("Attendance rules did not allow this punch.")
        return Outcome(ok=False, code="attendance_refused", message=message, status=403, direction=direction)
    record_attempt(employee, "verify", "success", request=request, direction=direction, score=outcome.score, activity=activity)
    if direction == "in":
        message = _("Checked in at %(time)s.") % {"time": activity.clock_in.strftime("%H:%M")}
    else:
        message = _("Checked out at %(time)s.") % {"time": (activity.clock_out or timezone.localtime().time()).strftime("%H:%M")}
    return Outcome(ok=True, code="success", message=message, direction=direction, score=outcome.score)


# Retention -----------------------------------------------------------------


def purge_expired_templates():
    """Delete templates past their company's retention period and those of inactive employees."""
    from employee.models import Employee

    deleted = 0
    employee_ids = list(FaceTemplate.all_objects.values_list("employee_id", flat=True).distinct())
    for employee in Employee._base_manager.filter(pk__in=employee_ids).select_related("employee_work_info"):
        if not employee.is_active:
            deleted += delete_templates(employee, "inactive")
        else:
            deleted += _delete_expired(employee)
    return deleted
