"""
Encryption at rest for face templates (same scheme as line_integration/crypto.py).

Key: ``settings.ORVAHR_FACE_ENCRYPTION_KEY`` when set, otherwise derived from
``SECRET_KEY`` with a context string of its own, so the LINE key and the face
key differ even when both come from SECRET_KEY.

The plaintext binds the vector to its owner: ``{"e": employee_id, "s":
schema, "v": [...]}``. A ciphertext copied onto another employee's row, or
into another tenant's schema, decrypts but is refused. Rotating the key makes
every template unreadable; employees then simply re-enrol.

Nothing in this module logs a vector or a ciphertext.
"""

import base64
import hashlib
import hmac
import json
import logging
import math

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

from horilla.tenancy import current_schema

logger = logging.getLogger(__name__)

_CONTEXT = b"orvahr.face.v1"


def _key_material():
    explicit = getattr(settings, "ORVAHR_FACE_ENCRYPTION_KEY", "") or ""
    return explicit or settings.SECRET_KEY


def _fernet():
    digest = hmac.new(_key_material().encode("utf-8"), _CONTEXT + b":fernet", hashlib.sha256).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_template(vector, employee_id):
    """Encrypt a list of floats for ``employee_id``. Returns an ASCII token."""
    values = [float(x) for x in vector]
    if not values or not all(math.isfinite(x) for x in values):
        raise ValueError("template must be a non-empty list of finite numbers")
    payload = json.dumps(
        {"e": int(employee_id), "s": current_schema() or "", "v": values}, separators=(",", ":")
    )
    return _fernet().encrypt(payload.encode("utf-8")).decode("ascii")


def decrypt_template(token, employee_id):
    """The float list, or None when unreadable or not bound to this employee/tenant."""
    if not token:
        return None
    try:
        data = json.loads(_fernet().decrypt(token.encode("ascii")).decode("utf-8"))
    except (InvalidToken, ValueError, TypeError):
        logger.error("A face template could not be decrypted (key changed?); the employee must re-enrol.")
        return None
    if data.get("e") != int(employee_id) or data.get("s", "") != (current_schema() or ""):
        logger.error("A face template is bound to another employee or tenant; refused.")
        return None
    values = data.get("v")
    if not isinstance(values, list) or not values:
        return None
    return values
