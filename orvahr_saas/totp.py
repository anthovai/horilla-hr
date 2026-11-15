"""
Time-based one-time passwords for console operators (RFC 6238 / RFC 4226),
implemented with the standard library -- no extra dependency.

* ``hotp(key, counter)`` -- RFC 4226 HOTP (dynamic truncation).
* ``totp(key, at)``      -- RFC 6238 TOTP (30-second steps from the Unix epoch).
* ``verify(secret_b32, code, at, last_counter)`` -- accepts the current step
  and one step either side (clock drift), refuses steps already used (replay),
  and returns the matched step so the caller can store it.
* Recovery codes: 80 random bits each, shown once, stored as salted SHA-256
  (fast hashing is fine for 80 bits of entropy; a password hash would make
  checking ten codes take seconds).

No QR library is in requirements.txt, so enrolment shows the ``otpauth://``
URI and the key in groups of four for manual entry; every authenticator app
accepts either.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

from django.utils.crypto import constant_time_compare

STEP_SECONDS = 30
DIGITS = 6
DRIFT_STEPS = 1
SECRET_BYTES = 20  # 160 bits, as RFC 4226 recommends for SHA-1
RECOVERY_CODE_COUNT = 10
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o/1/l/i

_DIGESTS = {"sha1": hashlib.sha1, "sha256": hashlib.sha256, "sha512": hashlib.sha512}


def hotp(key: bytes, counter: int, digits: int = DIGITS, algorithm="sha1") -> str:
    mac = hmac.new(key, struct.pack(">Q", counter), _DIGESTS[algorithm]).digest()
    offset = mac[-1] & 0x0F
    binary = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10**digits)).zfill(digits)


def counter_at(at: float | None = None, step: int = STEP_SECONDS) -> int:
    return int((time.time() if at is None else at) // step)


def totp(
    key: bytes,
    at: float | None = None,
    digits: int = DIGITS,
    algorithm="sha1",
    step: int = STEP_SECONDS,
) -> str:
    return hotp(key, counter_at(at, step), digits, algorithm)


# ------------------------------------------------------------------ secrets


def new_secret() -> str:
    """Random base32 secret (no padding), as authenticator apps expect."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode().rstrip("=")


def decode_secret(secret_b32: str) -> bytes:
    cleaned = "".join(secret_b32.split()).upper()
    return base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))


def grouped(secret_b32: str) -> str:
    """``ABCD EFGH ...`` for typing the key by hand."""
    return " ".join(secret_b32[i : i + 4] for i in range(0, len(secret_b32), 4))


def provisioning_uri(secret_b32: str, account: str, issuer="OrvaHR Console") -> str:
    label = quote(f"{issuer}:{account}", safe=":@")
    query = urlencode(
        {
            "secret": secret_b32,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": DIGITS,
            "period": STEP_SECONDS,
        }
    )
    return f"otpauth://totp/{label}?{query}"


def normalize_code(code: str) -> str:
    return "".join(ch for ch in (code or "") if not ch.isspace() and ch != "-")


def verify(
    secret_b32: str,
    code: str,
    at: float | None = None,
    last_counter: int | None = None,
    drift: int = DRIFT_STEPS,
) -> int | None:
    """
    The time step ``code`` belongs to, or None. Steps at or before
    ``last_counter`` (already used) never match.
    """
    code = normalize_code(code)
    if not secret_b32 or len(code) != DIGITS or not code.isdigit():
        return None
    try:
        key = decode_secret(secret_b32)
    except (ValueError, TypeError):
        return None
    now = counter_at(at)
    matched = None
    for counter in range(now - drift, now + drift + 1):
        # Compare every candidate (no early exit) so timing does not reveal
        # which window matched.
        if constant_time_compare(hotp(key, counter), code) and matched is None:
            matched = counter
    if matched is None or (last_counter is not None and matched <= last_counter):
        return None
    return matched


# ----------------------------------------------------------- recovery codes


def new_recovery_code() -> str:
    raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(16))
    return "-".join(raw[i : i + 4] for i in range(0, 16, 4))


def normalize_recovery_code(code: str) -> str:
    return normalize_code(code).lower()


def hash_recovery_code(code: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    digest = hashlib.sha256(
        f"{salt}:{normalize_recovery_code(code)}".encode()
    ).hexdigest()
    return f"sha256${salt}${digest}"


def recovery_code_matches(code: str, encoded: str) -> bool:
    try:
        algorithm, salt, _digest = encoded.split("$", 2)
    except ValueError:
        return False
    if algorithm != "sha256":
        return False
    return constant_time_compare(hash_recovery_code(code, salt), encoded)
