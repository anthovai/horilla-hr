"""
One-time link codes: an employee generates a code in OrvaHR and sends it to
the LINE bot; the signed webhook tells us which LINE userId sent it.

* codes are 8 chars from a 31-symbol alphabet (~8.5e11 space)
* only an HMAC of the code is stored
* single use, expire after CODE_TTL
* generation is rate-limited per employee, redemption per LINE user
"""

import re
import secrets
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from line_integration import crypto
from line_integration.models import (
    LineChannel,
    LineLinkAttempt,
    LineLinkCode,
    LineUserLink,
)

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
CODE_LENGTH = 8
CODE_TTL = timedelta(minutes=15)
MAX_CODES_PER_HOUR = 5
MAX_FAILED_ATTEMPTS = 5
FAILED_ATTEMPT_WINDOW = timedelta(minutes=15)

_CODE_CHARS = "[A-HJ-NP-Z2-9]"
_CODE_RE = re.compile(
    r"^(?:LINK\s*[:\-]?\s*)?(" + _CODE_CHARS + "{4}-?" + _CODE_CHARS + "{4})$"
)

# Redemption outcomes
LINKED = "linked"
INVALID = "invalid"
EXPIRED = "expired"
USED = "used"
RATE_LIMITED = "rate_limited"
TAKEN = "taken"  # LINE account already linked to another employee


class LinkCodeError(Exception):
    pass


class RateLimited(LinkCodeError):
    pass


class NoChannel(LinkCodeError):
    pass


def employee_company(employee):
    work_info = getattr(employee, "employee_work_info", None)
    return getattr(work_info, "company_id", None) if work_info else None


def channel_for_employee(employee):
    return LineChannel.for_company(employee_company(employee))


def generate_code(employee, now=None):
    """
    Create a fresh code for ``employee``. Returns (plain_code, LineLinkCode).
    Earlier unused codes are retired. Raises RateLimited / NoChannel.
    """
    now = now or timezone.now()
    channel = channel_for_employee(employee)
    if channel is None or not channel.is_configured:
        raise NoChannel()
    recent = LineLinkCode.objects.filter(
        employee=employee, created_at__gte=now - timedelta(hours=1)
    ).count()
    if recent >= MAX_CODES_PER_HOUR:
        raise RateLimited()

    LineLinkCode.objects.filter(employee=employee, used_at__isnull=True).update(used_at=now)
    for _attempt in range(5):
        raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
        try:
            with transaction.atomic():
                obj = LineLinkCode.objects.create(
                    employee=employee,
                    channel=channel,
                    code_hash=crypto.keyed_hash(raw),
                    created_at=now,
                    expires_at=now + CODE_TTL,
                )
            return format_code(raw), obj
        except IntegrityError:  # astronomically unlikely collision
            continue
    raise LinkCodeError("could not allocate a code")


def format_code(raw):
    return raw[:4] + "-" + raw[4:]


def normalise(text):
    """Return the bare 8-char code if ``text`` looks like one, else None."""
    if not text:
        return None
    candidate = re.sub(r"\s+", " ", str(text).strip().upper())
    match = _CODE_RE.match(candidate)
    if not match:
        return None
    return match.group(1).replace("-", "")


def _rate_limited(channel, line_user_id, now):
    return (
        LineLinkAttempt.objects.filter(
            channel=channel,
            line_user_id=line_user_id,
            created_at__gte=now - FAILED_ATTEMPT_WINDOW,
        ).count()
        >= MAX_FAILED_ATTEMPTS
    )


def _fail(channel, line_user_id, outcome, now):
    LineLinkAttempt.objects.create(channel=channel, line_user_id=line_user_id, created_at=now)
    return outcome, None


def redeem(channel, line_user_id, code, now=None):
    """
    Try to link ``line_user_id`` using ``code`` (already normalised).
    Returns (outcome, LineUserLink|None).
    """
    now = now or timezone.now()
    if _rate_limited(channel, line_user_id, now):
        return RATE_LIMITED, None

    with transaction.atomic():
        record = (
            LineLinkCode.objects.select_for_update()
            .select_related("employee")
            .filter(code_hash=crypto.keyed_hash(code), channel=channel)
            .first()
        )
        if record is None:
            return _fail(channel, line_user_id, INVALID, now)
        if record.used_at is not None:
            return _fail(channel, line_user_id, USED, now)
        if record.expires_at <= now:
            return _fail(channel, line_user_id, EXPIRED, now)

        other = (
            LineUserLink.objects.filter(channel=channel, line_user_id=line_user_id)
            .exclude(employee=record.employee)
            .exists()
        )
        if other:
            return TAKEN, None

        # Burn the code first; the conditional update makes a concurrent
        # second redemption lose even without row locks (SQLite).
        burned = LineLinkCode.objects.filter(pk=record.pk, used_at__isnull=True).update(
            used_at=now
        )
        if not burned:
            return _fail(channel, line_user_id, USED, now)

        link, _created = LineUserLink.objects.update_or_create(
            employee=record.employee,
            defaults={
                "channel": channel,
                "line_user_id": line_user_id,
                "is_active": True,
                "linked_at": now,
            },
        )
        LineLinkCode.objects.filter(employee=record.employee, used_at__isnull=True).update(
            used_at=now
        )
    return LINKED, link
