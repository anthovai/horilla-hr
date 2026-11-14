"""
Thai formatting helpers: Buddhist-era dates, baht in words, Thai ID numbers.

Pure Python (no Django imports) so they can be unit-tested and reused from
templates, PDFs and exports alike. The template filters in
``orvahr_th.templatetags.thai`` are thin wrappers around these.

``baht_text`` mirrors web/src/lib/baht-text.ts (the TypeScript twin takes
satang integers; this one takes baht as Decimal / int / float / str).
"""

import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

BE_OFFSET = 543

THAI_MONTHS = (
    "มกราคม",
    "กุมภาพันธ์",
    "มีนาคม",
    "เมษายน",
    "พฤษภาคม",
    "มิถุนายน",
    "กรกฎาคม",
    "สิงหาคม",
    "กันยายน",
    "ตุลาคม",
    "พฤศจิกายน",
    "ธันวาคม",
)

THAI_MONTHS_SHORT = (
    "ม.ค.",
    "ก.พ.",
    "มี.ค.",
    "เม.ย.",
    "พ.ค.",
    "มิ.ย.",
    "ก.ค.",
    "ส.ค.",
    "ก.ย.",
    "ต.ค.",
    "พ.ย.",
    "ธ.ค.",
)


# ---------------------------------------------------------------- dates


def to_date(value):
    """A ``date`` from a date, datetime or ISO ``YYYY-MM-DD`` string; else None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def be_year(value):
    """Buddhist-era year for a date/datetime/ISO string or a CE year int."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value + BE_OFFSET
    date = to_date(value)
    if date is None:
        return None
    return date.year + BE_OFFSET


def be_date(value, style="long"):
    """
    Thai date with the Buddhist-era year.

    ``long``  -> "14 พฤศจิกายน 2569"
    ``short`` -> "14 พ.ย. 69"
    Returns "" for anything that is not a date.
    """
    date = to_date(value)
    if date is None:
        return ""
    year = date.year + BE_OFFSET
    if style == "short":
        return f"{date.day} {THAI_MONTHS_SHORT[date.month - 1]} {year % 100:02d}"
    return f"{date.day} {THAI_MONTHS[date.month - 1]} {year}"


# ---------------------------------------------------------- baht in words

_DIGITS = ("ศูนย์", "หนึ่ง", "สอง", "สาม", "สี่", "ห้า", "หก", "เจ็ด", "แปด", "เก้า")
_PLACES = ("", "สิบ", "ร้อย", "พัน", "หมื่น", "แสน")
_MILLION = 1_000_000


def _below_million(n, has_higher):
    """
    Words for 0 < n < 1,000,000.

    ``has_higher``: a higher group (millions) is non-zero, so a lone units 1
    still reads เอ็ด ("หนึ่งล้านเอ็ด").
    """
    words = []
    digits = str(n)
    length = len(digits)
    for i, ch in enumerate(digits):
        d = int(ch)
        place = length - 1 - i
        if d == 0:
            continue
        if place == 0:
            if d == 1 and (n > 9 or has_higher):
                words.append("เอ็ด")
            else:
                words.append(_DIGITS[d])
        elif place == 1:
            if d == 1:
                words.append("สิบ")
            elif d == 2:
                words.append("ยี่สิบ")
            else:
                words.append(_DIGITS[d] + "สิบ")
        else:
            words.append(_DIGITS[d] + _PLACES[place])
    return "".join(words)


def number_text(n):
    """Thai words for a non-negative integer, with repeated ล้าน above 10^12."""
    if n < 0:
        raise ValueError("number_text takes a non-negative integer")
    if n == 0:
        return _DIGITS[0]
    return _number_text(n, has_higher=False)


def _number_text(n, has_higher):
    high, low = divmod(n, _MILLION)
    out = ""
    if high:
        out = _number_text(high, has_higher) + "ล้าน"
    if low:
        out += _below_million(low, has_higher or bool(high))
    return out


def to_decimal(value):
    """
    ``Decimal`` from Decimal / int / float / numeric str.

    Floats go through ``str`` (shortest repr), so 1.005 is 1.005 rather than
    1.00499999999999989... and rounds as a person would expect.
    Raises ValueError for bools, None, NaN, infinities and junk.
    """
    if value is None or isinstance(value, bool):
        raise ValueError(f"not an amount: {value!r}")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        result = Decimal(str(value))
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip().replace(",", ""))
        except InvalidOperation as exc:
            raise ValueError(f"not an amount: {value!r}") from exc
    else:
        raise ValueError(f"not an amount: {value!r}")
    if not result.is_finite():
        raise ValueError(f"not a finite amount: {value!r}")
    return result


def to_satang(value):
    """Amount in baht -> integer satang, rounded half-up (away from zero)."""
    amount = to_decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(amount * 100)


def baht_text(value):
    """
    Baht amount in Thai words, as written on cheques and payslips.

    1234.5  -> "หนึ่งพันสองร้อยสามสิบสี่บาทห้าสิบสตางค์"
    21      -> "ยี่สิบเอ็ดบาทถ้วน"
    0.01    -> "ศูนย์บาทหนึ่งสตางค์"
    -1      -> "ลบหนึ่งบาทถ้วน"
    Rounded to the satang, half-up, before converting.
    """
    satang_total = to_satang(value)
    sign = "ลบ" if satang_total < 0 else ""
    baht, satang = divmod(abs(satang_total), 100)
    text = number_text(baht) + "บาท"
    if satang:
        text += _below_million(satang, has_higher=False) + "สตางค์"
    else:
        text += "ถ้วน"
    return sign + text


# ------------------------------------------------------------- Thai IDs


def _id_digits(value):
    if value is None:
        return ""
    return "".join(ch for ch in str(value) if not ch.isspace() and ch != "-")


def format_thai_id(value):
    """
    13-digit Thai national / tax ID as 1-2345-67890-12-1.

    Anything that is not 13 digits (after removing spaces and dashes) is
    returned unchanged, so a bad value is visible rather than reshaped.
    """
    digits = _id_digits(value)
    if len(digits) != 13 or not digits.isdigit():
        return "" if value is None else str(value)
    return f"{digits[0]}-{digits[1:5]}-{digits[5:10]}-{digits[10:12]}-{digits[12]}"


def mask_thai_id(value, visible=4, mask_char="X"):
    """
    Thai ID with all but the last ``visible`` digits masked, keeping the
    1-4-5-2-1 grouping: X-XXXX-XXXX0-12-1. Non-13-digit values are masked
    whole except their last ``visible`` characters. Empty -> "".
    """
    digits = _id_digits(value)
    if not digits:
        return ""
    visible = max(0, min(visible, len(digits)))
    masked = mask_char * (len(digits) - visible) + digits[len(digits) - visible :]
    if len(digits) != 13 or not digits.isdigit():
        return masked
    m = masked
    return f"{m[0]}-{m[1:5]}-{m[5:10]}-{m[10:12]}-{m[12]}"
