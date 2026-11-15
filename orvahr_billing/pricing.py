"""
Prices for the public plan table (``orvahr_saas.plans.plan_prices`` looks up
``orvahr_billing.pricing.public_prices``).
"""

from django.utils import timezone
from django.utils.translation import gettext as _

from orvahr_billing.models import BillingCycle, PlanPrice


def _money(value):
    return f"฿{value:,.0f}" if value == value.to_integral_value() else f"฿{value:,.2f}"


def price_label(price):
    per = _("month") if price.cycle == BillingCycle.MONTHLY else _("year")
    if price.per_employee_price and price.base_price:
        text = _("%(base)s + %(each)s per employee") % {
            "base": _money(price.base_price),
            "each": _money(price.per_employee_price),
        }
    elif price.per_employee_price:
        text = _("%(each)s per employee") % {"each": _money(price.per_employee_price)}
    else:
        text = _money(price.base_price)
    return _("%(price)s / %(period)s, excl. VAT") % {"price": text, "period": per}


def public_prices(plans):
    """{plan_code: label} for the plans that have a current price."""
    today = timezone.localdate()
    result = {}
    for plan in plans:
        code = getattr(plan, "code", plan)
        price = PlanPrice.current(
            code, BillingCycle.MONTHLY, today
        ) or PlanPrice.current(code, BillingCycle.YEARLY, today)
        if price is not None:
            result[code] = price_label(price)
    return result
