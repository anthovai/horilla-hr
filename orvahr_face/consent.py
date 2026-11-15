"""
Biometric consent (PDPA s.26) and sensitive-data access logging for face check-in.

Consent lives in orvahr_pdpa as the employee purpose ``biometric_face``
(seeded by migration 0002). It is optional in orvahr_pdpa's sense: the
employee grants and withdraws it at any time, on the face page or on
"My Privacy". Enrolment and verification require an active grant;
withdrawing it deletes the templates (orvahr_face.signals).

Without orvahr_pdpa installed there is nowhere to keep consent evidence, so
face check-in refuses to enrol anyone.
"""

import logging

from django.apps import apps

logger = logging.getLogger(__name__)

PURPOSE_CODE = "biometric_face"

PURPOSE_NAME = "Face check-in (biometric data)"
PURPOSE_NAME_TH = "การลงเวลาด้วยใบหน้า (ข้อมูลชีวภาพ)"
PURPOSE_TEXT = (
    "ขอความยินยอมโดยชัดแจ้ง (พ.ร.บ. คุ้มครองข้อมูลส่วนบุคคล พ.ศ. 2562 มาตรา 26)\n"
    "วัตถุประสงค์: บริษัทจะสร้างแม่แบบใบหน้า (ค่าตัวเลขที่เข้ารหัส ไม่ใช่รูปถ่าย) จากภาพกล้องของท่าน "
    "เพื่อใช้ยืนยันตัวตนเมื่อลงเวลาเข้า-ออกงานเท่านั้น ไม่ใช้เพื่อวัตถุประสงค์อื่น\n"
    "ระบบไม่เก็บรูปภาพ: ภาพจากกล้องถูกส่งไปประมวลผลและถูกทิ้งทันที\n"
    "ระยะเวลาเก็บ: แม่แบบใบหน้าถูกเก็บไม่เกินระยะเวลาที่บริษัทกำหนด (ค่าเริ่มต้น 2 ปี) "
    "และถูกลบทันทีเมื่อท่านถอนความยินยอม หรือเมื่อท่านพ้นสภาพพนักงาน\n"
    "ทางเลือก: การให้ความยินยอมเป็นความสมัครใจ หากไม่ยินยอม ท่านยังลงเวลาด้วยวิธีปกติ "
    "(ปุ่มลงเวลาหรือช่องทางอื่นที่บริษัทกำหนด) ได้โดยไม่มีผลเสียใดๆ "
    "และถอนความยินยอมได้ทุกเมื่อที่หน้าลงเวลาด้วยใบหน้าหรือหน้า \"ความเป็นส่วนตัวของฉัน\"\n"
    "\n"
    "Explicit consent (Thai PDPA B.E. 2562, section 26)\n"
    "Purpose: the company creates a face template (encrypted numbers, not a photo) from your camera "
    "images and uses it only to confirm it is you when you check in or out. It is not used for "
    "anything else.\n"
    "No photos are stored: camera images are processed and discarded immediately.\n"
    "Retention: templates are kept no longer than the period your company sets (default 2 years) and "
    "are deleted at once when you withdraw consent or leave the company.\n"
    "Alternative: consent is voluntary. Without it you keep checking in the normal way (the check-in "
    "button or another method your company provides) with no disadvantage, and you can withdraw at "
    "any time on the face check-in page or on \"My Privacy\"."
)


def pdpa_available():
    return apps.is_installed("orvahr_pdpa")


def _company(employee):
    work_info = getattr(employee, "employee_work_info", None) if employee else None
    return getattr(work_info, "company_id", None) if work_info else None


def purpose_for(employee):
    """The biometric purpose applying to ``employee`` (company's own first, else global)."""
    if not pdpa_available():
        return None
    from orvahr_pdpa.models import ConsentPurpose

    company = _company(employee)
    qs = ConsentPurpose.all_objects.filter(audience="employee", code=PURPOSE_CODE, is_active=True)
    own = qs.filter(company=company).first() if company else None
    return own or qs.filter(company__isnull=True).first()


def _purpose_ids():
    from orvahr_pdpa.models import ConsentPurpose

    return ConsentPurpose.all_objects.filter(code=PURPOSE_CODE, audience="employee").values("pk")


def latest_record(employee):
    if not pdpa_available() or employee is None:
        return None
    from orvahr_pdpa.models import ConsentRecord

    return (
        ConsentRecord.all_objects.filter(employee=employee, purpose__in=_purpose_ids())
        .order_by("-created_at", "-id")
        .first()
    )


def has_consent(employee):
    """True only when the latest biometric record of ``employee`` is a grant."""
    record = latest_record(employee)
    return record is not None and record.action == "granted"


def grant(employee, request=None, channel="web"):
    """Record an explicit grant. Returns True when consent is now active."""
    purpose = purpose_for(employee)
    if purpose is None:
        return False
    from orvahr_pdpa.consent import set_employee_consent

    set_employee_consent(employee, purpose, True, request=request, channel=channel)
    return has_consent(employee)


def withdraw(employee, request=None, channel="web"):
    """Record a withdrawal. The post_save signal deletes the templates."""
    purpose = purpose_for(employee)
    if purpose is None:
        return False
    from orvahr_pdpa.consent import set_employee_consent

    set_employee_consent(employee, purpose, False, request=request, channel=channel)
    return not has_consent(employee)


def is_biometric_record(record):
    """For the ConsentRecord post_save signal."""
    purpose = getattr(record, "purpose", None)
    return purpose is not None and purpose.code == PURPOSE_CODE


def log_access(request, employee, fields, context):
    """SensitiveAccessLog row (field names only). Never raises."""
    if not pdpa_available() or employee is None:
        return None
    try:
        from orvahr_pdpa.access_log import log_sensitive_access

        return log_sensitive_access(request, employee, fields, context=context)
    except Exception:
        logger.exception("Face check-in sensitive access log failed")
        return None
