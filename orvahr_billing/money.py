"""
Money arithmetic for billing documents.

Every amount is a ``Decimal`` in baht, rounded half-up to the satang (0.01)
at each step a figure is printed on a document: line amounts, subtotal, VAT,
withholding tax. Rounding the printed figures (not an unrounded running
total) keeps every document arithmetically checkable by the customer's
accountant: subtotal + VAT == total, exactly.
"""

from decimal import ROUND_HALF_UP, Decimal

from orvahr_th.formatting import to_decimal

SATANG = Decimal("0.01")
ZERO = Decimal("0.00")
HUNDRED = Decimal("100")


def q(value) -> Decimal:
    """Round to the satang, half-up (0.005 -> 0.01, -0.005 -> -0.01)."""
    return to_decimal(value).quantize(SATANG, rounding=ROUND_HALF_UP)


def line_amount(quantity, unit_price) -> Decimal:
    return q(to_decimal(quantity) * to_decimal(unit_price))


def percent_of(base, rate) -> Decimal:
    """``rate`` percent of ``base``, rounded half-up to the satang."""
    return q(to_decimal(base) * to_decimal(rate) / HUNDRED)


def vat_on(base, rate) -> Decimal:
    """VAT on a VAT-exclusive ``base`` at ``rate`` percent."""
    return percent_of(base, rate)


def wht_on(base, rate) -> Decimal:
    """
    Withholding tax the customer deducts: ``rate`` percent of the
    VAT-exclusive service value (Thai WHT is never levied on the VAT).
    """
    return percent_of(base, rate)


def split_vat_inclusive(gross, rate):
    """
    Split a VAT-inclusive amount into (base, vat) so that base + vat == gross
    exactly. The base is rounded; the VAT is the remainder.
    """
    gross = q(gross)
    rate = to_decimal(rate)
    if not rate:
        return gross, ZERO
    base = q(gross * HUNDRED / (HUNDRED + rate))
    return base, gross - base
