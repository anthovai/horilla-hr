"""
Operator two-factor authentication: enrolment, code checks, recovery codes,
lockout. The console views (orvahr_saas.console) call these; every change
lands in the OperatorAction audit trail.

* A code is accepted once: the matched TOTP step is stored and an equal or
  older step is refused (conditional UPDATE, so two parallel requests cannot
  both use it).
* ORVAHR_CONSOLE_2FA_FAILURES wrong codes (default 5) lock second-factor
  checks for that operator for ORVAHR_CONSOLE_2FA_LOCK_SECONDS (900 s);
  the lock is audited (``2fa_locked``).
* Lost device: ``create_console_operator --username X --reset-2fa`` (also
  ends the operator's sessions).
"""

from __future__ import annotations

from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from orvahr_saas import conf, totp
from orvahr_saas.services import record_action


def _models():
    from orvahr_saas import models

    return models


# ----------------------------------------------------------------- lockout


def _fail_key(operator):
    return f"orvahr_saas:console-2fa-fail:{operator.pk}"


def is_locked(operator) -> bool:
    return (cache.get(_fail_key(operator)) or 0) >= conf.two_factor_failures()


def register_failure(operator, *, ip_address=None, reason="totp"):
    key = _fail_key(operator)
    seconds = conf.two_factor_lock_seconds()
    if cache.add(key, 1, seconds):
        count = 1
    else:
        try:
            count = cache.incr(key)
        except ValueError:
            cache.set(key, 1, seconds)
            count = 1
    record_action("2fa_failed", operator=operator, ip_address=ip_address, method=reason)
    if count == conf.two_factor_failures():
        record_action(
            "2fa_locked",
            operator=operator,
            ip_address=ip_address,
            seconds=seconds,
        )
    return count


def clear_failures(operator):
    cache.delete(_fail_key(operator))


# --------------------------------------------------------------- checking


def _use_counter(operator, counter) -> bool:
    """Store ``counter`` as the last used step unless a newer one was used."""
    m = _models()
    updated = (
        m.ConsoleOperator.objects.filter(pk=operator.pk)
        .filter(Q(totp_last_counter__isnull=True) | Q(totp_last_counter__lt=counter))
        .update(totp_last_counter=counter)
    )
    if updated:
        operator.totp_last_counter = counter
    return bool(updated)


def check_totp(operator, code, at=None) -> bool:
    if not operator.has_totp:
        return False
    counter = totp.verify(
        operator.totp_secret, code, at=at, last_counter=operator.totp_last_counter
    )
    return counter is not None and _use_counter(operator, counter)


def looks_like_recovery_code(code) -> bool:
    cleaned = totp.normalize_code(code)
    return len(cleaned) == 16 and not cleaned.isdigit()


def use_recovery_code(operator, code) -> bool:
    m = _models()
    for row in m.OperatorRecoveryCode.objects.filter(
        operator=operator, used_at__isnull=True
    ):
        if totp.recovery_code_matches(code, row.code_hash):
            return bool(
                m.OperatorRecoveryCode.objects.filter(
                    pk=row.pk, used_at__isnull=True
                ).update(used_at=timezone.now())
            )
    return False


def remaining_recovery_codes(operator) -> int:
    return operator.recovery_codes.filter(used_at__isnull=True).count()


def verify_second_factor(operator, code, *, ip_address=None, at=None):
    """
    ``(ok, method, error)`` for a sign-in code (TOTP or a recovery code).
    Applies the lockout; audits failures and the lock.
    """
    if is_locked(operator):
        return False, None, "locked"
    if looks_like_recovery_code(code):
        if use_recovery_code(operator, code):
            clear_failures(operator)
            return True, "recovery_code", None
        register_failure(operator, ip_address=ip_address, reason="recovery_code")
        return False, None, "invalid"
    if check_totp(operator, code, at=at):
        clear_failures(operator)
        return True, "totp", None
    register_failure(operator, ip_address=ip_address, reason="totp")
    return False, None, "locked" if is_locked(operator) else "invalid"


# --------------------------------------------------------------- enrolment


def new_recovery_codes(operator, *, ip_address=None, audit=True):
    """Replace the operator's recovery codes; returns the plain codes (show once)."""
    m = _models()
    codes = [totp.new_recovery_code() for _ in range(totp.RECOVERY_CODE_COUNT)]
    with transaction.atomic():
        m.OperatorRecoveryCode.objects.filter(operator=operator).delete()
        m.OperatorRecoveryCode.objects.bulk_create(
            [
                m.OperatorRecoveryCode(
                    operator=operator, code_hash=totp.hash_recovery_code(code)
                )
                for code in codes
            ]
        )
    if audit:
        record_action(
            "2fa_recovery_codes_regenerated", operator=operator, ip_address=ip_address
        )
    return codes


def enrol(operator, secret, code, *, ip_address=None, at=None):
    """
    Confirm ``secret`` with a code from the app. Returns the ten recovery
    codes on success, None when the code is wrong.
    """
    counter = totp.verify(secret, code, at=at)
    if counter is None:
        return None
    m = _models()
    with transaction.atomic():
        operator.totp_secret = secret
        operator.totp_enabled_at = timezone.now()
        operator.totp_last_counter = counter
        m.ConsoleOperator.objects.filter(pk=operator.pk).update(
            totp_secret=secret,
            totp_enabled_at=operator.totp_enabled_at,
            totp_last_counter=counter,
        )
        codes = new_recovery_codes(operator, audit=False)
        record_action("2fa_enrolled", operator=operator, ip_address=ip_address)
    clear_failures(operator)
    return codes


def disable(operator, *, operator_acting=None, actor_label="", ip_address=None):
    """Remove 2FA (CLI reset or the operator's own choice when not mandatory)."""
    m = _models()
    with transaction.atomic():
        m.ConsoleOperator.objects.filter(pk=operator.pk).update(
            totp_secret="", totp_enabled_at=None, totp_last_counter=None
        )
        m.OperatorRecoveryCode.objects.filter(operator=operator).delete()
        operator.totp_secret = ""
        operator.totp_enabled_at = None
        operator.totp_last_counter = None
        record_action(
            "2fa_disabled",
            operator=operator_acting,
            actor_label=actor_label,
            ip_address=ip_address,
            username=operator.username,
        )
    clear_failures(operator)
