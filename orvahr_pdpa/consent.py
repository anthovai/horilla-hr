"""
Consent helpers. Every change appends a ConsentRecord; nothing is updated in
place, so the table is the evidence trail.

Call sites outside this app (not wired here, see the app report):

* login / first visit:  ``needs_acknowledgement(employee)`` then redirect to
  ``reverse("orvahr_pdpa:my-privacy")``
* candidate apply form: ``record_candidate_consent(candidate, purpose_ids,
  request=request)`` after the Candidate is saved
"""

from django.db import transaction

from horilla.client_ip import client_ip
from orvahr_pdpa.models import (
    ConsentPurpose,
    ConsentRecord,
    PrivacyNotice,
    employee_company,
)


def _ip(request):
    if request is None:
        return None
    try:
        return client_ip(request) or None
    except Exception:
        return None


def _user(request):
    user = getattr(request, "user", None)
    return user if user is not None and user.is_authenticated else None


def current_notice_for_employee(employee):
    return PrivacyNotice.current("employee", employee_company(employee))


def purposes_for_employee(employee):
    return ConsentPurpose.for_subject("employee", employee_company(employee))


def latest_states(records):
    """{purpose_id or None: latest ConsentRecord} from a newest-first iterable."""
    states = {}
    for record in records:
        states.setdefault(record.purpose_id, record)
    return states


def employee_states(employee):
    return latest_states(
        ConsentRecord.all_objects.filter(employee=employee)
        .select_related("purpose", "notice")
        .order_by("-created_at", "-id")
    )


def has_acknowledged(employee, notice):
    if notice is None:
        return True
    return ConsentRecord.all_objects.filter(
        employee=employee, notice=notice, purpose__isnull=True, action="acknowledged"
    ).exists()


def needs_acknowledgement(employee):
    """True when a published employee notice exists that the employee has not acknowledged."""
    if employee is None:
        return False
    try:
        return not has_acknowledged(employee, current_notice_for_employee(employee))
    except Exception:
        return False


def _create(*, employee=None, candidate=None, company=None, notice, purpose, action, channel, request):
    return ConsentRecord.all_objects.create(
        company=company,
        employee=employee,
        candidate=candidate,
        notice=notice,
        notice_version=notice.version if notice else "",
        purpose=purpose,
        action=action,
        channel=channel,
        recorded_by=_user(request),
        ip_address=_ip(request),
    )


def acknowledge_notice(employee, notice, *, request=None, channel="web"):
    if notice is None or has_acknowledged(employee, notice):
        return None
    return _create(
        employee=employee,
        company=employee_company(employee),
        notice=notice,
        purpose=None,
        action="acknowledged",
        channel=channel,
        request=request,
    )


def set_employee_consent(employee, purpose, grant, *, request=None, channel="web"):
    """Grant or withdraw one optional purpose. No-op if already in that state."""
    if not purpose.optional and not grant:
        raise ValueError("Only optional purposes can be withdrawn here.")
    action = "granted" if grant else "withdrawn"
    latest = (
        ConsentRecord.all_objects.filter(employee=employee, purpose=purpose)
        .order_by("-created_at", "-id")
        .first()
    )
    if latest is not None and latest.action == action:
        return None
    if latest is None and not grant:
        return None
    return _create(
        employee=employee,
        company=employee_company(employee),
        notice=current_notice_for_employee(employee),
        purpose=purpose,
        action=action,
        channel=channel,
        request=request,
    )


@transaction.atomic
def record_candidate_consent(candidate, purpose_ids=(), *, request=None, channel="candidate_apply"):
    """
    Record that a candidate saw the current candidate notice, plus any
    optional purposes they ticked. Returns the created records.
    """
    recruitment = getattr(candidate, "recruitment_id", None)
    company = getattr(recruitment, "company_id", None)
    notice = PrivacyNotice.current("candidate", company)
    created = []
    if notice is not None:
        created.append(
            _create(candidate=candidate, company=company, notice=notice, purpose=None,
                    action="acknowledged", channel=channel, request=request)
        )
    allowed = ConsentPurpose.for_subject("candidate", company).filter(pk__in=list(purpose_ids))
    for purpose in allowed:
        created.append(
            _create(candidate=candidate, company=company, notice=notice, purpose=purpose,
                    action="granted", channel=channel, request=request)
        )
    return created
