"""
Control-plane settings with their defaults. Each one is read from Django
settings, else from the environment of the same name, else the default --
so they can be set without editing the settings package.

==========================================  ====================================
ORVAHR_CONSOLE_REQUIRE_2FA                  True: operators must use TOTP
ORVAHR_CONSOLE_REQUIRE_2FA_FROM             ISO date; before it 2FA is optional
                                            (empty: required now)
ORVAHR_CONSOLE_2FA_FAILURES                 5 wrong codes ...
ORVAHR_CONSOLE_2FA_LOCK_SECONDS             ... lock 2FA for 900 s
ORVAHR_SAAS_ALLOWED_WHEN_LOCKED             path prefixes a trial-expired tenant
                                            still serves
ORVAHR_SAAS_TRIAL_REMINDER_DAYS             [7, 3, 1] days before the trial ends
ORVAHR_SAAS_NOTIFICATIONS                   True: send control-plane e-mails
ORVAHR_SAAS_NOTIFICATIONS_DISABLED          events not to send, e.g.
                                            ["invoice_issued"]
ORVAHR_SAAS_OPERATOR_EMAILS                 extra operator addresses
ORVAHR_SAAS_EMAIL_BACKEND                   else ORVAHR_BILLING_EMAIL_BACKEND,
                                            else EMAIL_BACKEND (Horilla's
                                            tenant-table backend -> SMTP)
ORVAHR_SAAS_FROM_EMAIL                      else ORVAHR_BILLING_FROM_EMAIL,
                                            else DEFAULT_FROM_EMAIL
ORVAHR_SAAS_CUSTOMER_LANGUAGE               "th" (language of customer mail)
ORVAHR_SAAS_OPERATOR_LANGUAGE               "en"
ORVAHR_SAAS_URL_SCHEME                      "https" (links in e-mails)
ORVAHR_SAAS_SUPPORT_EMAIL                   shown on the trial-ended page/mails
==========================================  ====================================
"""

from __future__ import annotations

import datetime
import json
import os

from django.conf import settings

DEFAULT_ALLOWED_WHEN_LOCKED = [
    "/login/",
    "/logout/",
    "/subscription/",
    "/static/",
    "/media/",
]

_MISSING = object()


def _raw(name, default):
    value = getattr(settings, name, _MISSING)
    if value is not _MISSING:
        return value
    return os.environ.get(name, default)


def _bool(name, default):
    value = _raw(name, default)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _int(name, default):
    try:
        return int(_raw(name, default))
    except (TypeError, ValueError):
        return default


def _list(name, default):
    value = _raw(name, default)
    if isinstance(value, str):
        value = value.strip()
        if value.startswith("["):
            try:
                return list(json.loads(value))
            except ValueError:
                return list(default)
        return [part.strip() for part in value.split(",") if part.strip()]
    return list(value or [])


def require_2fa_enabled() -> bool:
    return _bool("ORVAHR_CONSOLE_REQUIRE_2FA", True)


def require_2fa_from() -> datetime.date | None:
    value = _raw("ORVAHR_CONSOLE_REQUIRE_2FA_FROM", "")
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value).strip())
    except ValueError:
        return None  # unreadable date: fail closed (required now)


def two_factor_required(today: datetime.date | None = None) -> bool:
    if not require_2fa_enabled():
        return False
    start = require_2fa_from()
    if start is None:
        return True
    from django.utils import timezone

    return (today or timezone.localdate()) >= start


def two_factor_failures() -> int:
    return max(_int("ORVAHR_CONSOLE_2FA_FAILURES", 5), 1)


def two_factor_lock_seconds() -> int:
    return max(_int("ORVAHR_CONSOLE_2FA_LOCK_SECONDS", 15 * 60), 60)


def allowed_when_locked() -> list[str]:
    return _list("ORVAHR_SAAS_ALLOWED_WHEN_LOCKED", DEFAULT_ALLOWED_WHEN_LOCKED)


def trial_reminder_days() -> list[int]:
    days = []
    for value in _list("ORVAHR_SAAS_TRIAL_REMINDER_DAYS", [7, 3, 1]):
        try:
            if int(value) > 0:
                days.append(int(value))
        except (TypeError, ValueError):
            continue
    return sorted(set(days))


def notifications_enabled() -> bool:
    return _bool("ORVAHR_SAAS_NOTIFICATIONS", True)


def disabled_events() -> set[str]:
    return set(_list("ORVAHR_SAAS_NOTIFICATIONS_DISABLED", []))


def extra_operator_emails() -> list[str]:
    return _list("ORVAHR_SAAS_OPERATOR_EMAILS", [])


def email_backend() -> str:
    backend = _raw("ORVAHR_SAAS_EMAIL_BACKEND", "") or _raw(
        "ORVAHR_BILLING_EMAIL_BACKEND", ""
    )
    if not backend:
        backend = settings.EMAIL_BACKEND
        # Horilla's backend reads the mail server from tenant tables, which
        # the public schema does not have.
        if backend == "base.backends.ConfiguredEmailBackend":
            backend = "django.core.mail.backends.smtp.EmailBackend"
    return backend


def from_email() -> str:
    return (
        _raw("ORVAHR_SAAS_FROM_EMAIL", "")
        or _raw("ORVAHR_BILLING_FROM_EMAIL", "")
        or settings.DEFAULT_FROM_EMAIL
    )


def customer_language() -> str:
    return str(_raw("ORVAHR_SAAS_CUSTOMER_LANGUAGE", "th") or "th")


def operator_language() -> str:
    return str(_raw("ORVAHR_SAAS_OPERATOR_LANGUAGE", "en") or "en")


def url_scheme() -> str:
    return str(_raw("ORVAHR_SAAS_URL_SCHEME", "https") or "https")


def support_email() -> str:
    return str(_raw("ORVAHR_SAAS_SUPPORT_EMAIL", "") or "")
