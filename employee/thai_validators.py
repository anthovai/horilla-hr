"""Validators and helpers for Thai identity data (PDPA-sensitive)."""

import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

_DIGITS_13 = re.compile(r"^\d{13}$")


def normalize_digits(value):
    """Strip common separators (spaces, dashes) from an ID typed by a person."""
    if value is None:
        return None
    return re.sub(r"[\s-]", "", str(value))


def thai_national_id_checksum_digit(first_twelve):
    """Return the expected 13th digit for the first 12 digits (mod-11 rule)."""
    total = sum(int(d) * (13 - i) for i, d in enumerate(first_twelve))
    return (11 - total % 11) % 10


def is_valid_thai_national_id(value):
    """True if ``value`` is 13 digits with a correct mod-11 check digit."""
    value = normalize_digits(value)
    if not value or not _DIGITS_13.match(value):
        return False
    return thai_national_id_checksum_digit(value[:12]) == int(value[12])


def validate_thai_national_id(value):
    """Django validator: 13 digits and valid check digit."""
    value = normalize_digits(value)
    if not value or not _DIGITS_13.match(value):
        raise ValidationError(_("Must be exactly 13 digits."), code="invalid_length")
    if not is_valid_thai_national_id(value):
        raise ValidationError(_("Invalid check digit."), code="invalid_checksum")


def mask_id(value, visible=4):
    """Mask all but the last ``visible`` characters, e.g. 'XXXXXXXXX1234'."""
    if not value:
        return ""
    value = str(value)
    return "X" * max(len(value) - visible, 0) + value[-visible:]
