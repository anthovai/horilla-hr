"""
Encryption at rest for LINE channel credentials.

The WhatsApp integration stores its Meta token in plain text. A LINE channel
access token lets anyone message every employee who follows the official
account, and the channel secret is what authenticates our webhook, so both
are encrypted here with Fernet (AES-128-CBC + HMAC-SHA256).

Key: ``settings.LINE_INTEGRATION_ENCRYPTION_KEY`` when set (a Fernet key, or
any string, which is stretched), otherwise derived from ``SECRET_KEY``.
Rotating the key makes stored credentials unreadable; they then behave as
"not configured" (webhooks fail closed, sends are skipped) until re-entered.
"""

import base64
import hashlib
import hmac
import logging

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

logger = logging.getLogger(__name__)

_CONTEXT = b"orvahr.line_integration.v1"


def _key_material():
    explicit = getattr(settings, "LINE_INTEGRATION_ENCRYPTION_KEY", "") or ""
    return explicit or settings.SECRET_KEY


def _fernet():
    digest = hmac.new(
        _key_material().encode("utf-8"), _CONTEXT + b":fernet", hashlib.sha256
    ).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value):
    """Encrypt a str; empty input stays empty."""
    if not value:
        return ""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(token):
    """Decrypt to str; returns "" (and logs, without the value) on failure."""
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        logger.error(
            "LINE credential could not be decrypted (key changed?); "
            "re-enter it in LINE settings."
        )
        return ""


def keyed_hash(value):
    """Deterministic HMAC used to look up one-time link codes without storing them."""
    return hmac.new(
        _key_material().encode("utf-8"),
        _CONTEXT + b":code:" + value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def mask(value, visible=4):
    """'••••••••abcd' style mask for display. Never returns the full value."""
    if not value:
        return ""
    if len(value) <= visible * 2:
        return "•" * 8
    return "•" * 8 + value[-visible:]
