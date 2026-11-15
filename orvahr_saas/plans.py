"""
Plan features inside a tenant: which optional modules are on, and prices for
the public site.

Everything here is safe to import with ORVAHR_MULTI_TENANT off (orvahr_saas
is then not an installed app): models are imported lazily and every helper
answers "enabled / unlimited" outside SaaS mode, on the public schema, and for
a tenant that has no plan.
"""

from __future__ import annotations

import logging
from importlib import import_module

from django.utils.translation import gettext_lazy as _

from horilla.tenancy import current_tenant, in_public_schema, is_multi_tenant

logger = logging.getLogger(__name__)

# Optional modules a plan can switch on or off: app label -> label shown to
# people. Everything else (base, employee, reports, PDPA, settings...) is
# always available -- PDPA tooling in particular is a legal duty, not an
# upsell.
MODULE_CATALOG = {
    "attendance": _("Attendance"),
    "leave": _("Leave"),
    "payroll": _("Payroll"),
    "orvahr_benefits": _("Thai benefits"),
    "recruitment": _("Recruitment"),
    "onboarding": _("Onboarding"),
    "offboarding": _("Offboarding"),
    "pms": _("Performance"),
    "project": _("Projects"),
    "helpdesk": _("Helpdesk"),
    "asset": _("Assets"),
}

# First URL segment -> module, where the mount point is not the app label.
_PREFIX_ALIASES = {"th-benefits": "orvahr_benefits"}


def tenant_plan(tenant=None):
    """The active tenant's Plan, or None (single-tenant, public, no plan)."""
    if not is_multi_tenant():
        return None
    tenant = tenant if tenant is not None else current_tenant()
    if tenant is None or not getattr(tenant, "plan_id", None):
        return None
    try:
        return tenant.plan
    except Exception:  # noqa: BLE001 - a missing row must not break pages
        logger.exception("Could not load the plan of tenant %s", tenant)
        return None


def module_enabled(label: str, tenant=None) -> bool:
    """False only for a catalogued module left out of the tenant's plan."""
    if label not in MODULE_CATALOG:
        return True
    if not is_multi_tenant() or (tenant is None and in_public_schema()):
        return True
    plan = tenant_plan(tenant)
    return plan is None or plan.has_module(label)


def filter_enabled_apps(app_labels):
    """``settings.SIDEBARS`` minus the modules the active tenant's plan lacks."""
    if not is_multi_tenant():
        return app_labels
    plan = tenant_plan()
    if plan is None:
        return app_labels
    return [a for a in app_labels if a not in MODULE_CATALOG or plan.has_module(a)]


def module_for_path(path: str):
    """
    The catalogued module a URL belongs to, from its first segment:
    ``/payroll/...`` and the REST API's ``/api/payroll/...`` /
    ``/api/v1/payroll/...``. None for anything else.
    """
    parts = [p for p in path.split("/") if p]
    if parts[:1] == ["api"]:
        parts = parts[2:] if parts[1:2] == ["v1"] else parts[1:]
    if not parts:
        return None
    first = _PREFIX_ALIASES.get(parts[0], parts[0])
    return first if first in MODULE_CATALOG else None


def module_for_view(view_func):
    """
    The catalogued module a view function lives in -- catches views of a
    gated app mounted outside its own prefix (and ``horilla_api.api_views.
    <module>``). None when unknown.
    """
    view_class = getattr(view_func, "view_class", None) or getattr(
        view_func, "cls", None
    )
    module = getattr(view_class or view_func, "__module__", "") or ""
    parts = module.split(".")
    if parts[:2] == ["horilla_api", "api_views"] and len(parts) > 2:
        label = parts[2]
    else:
        label = parts[0]
    return label if label in MODULE_CATALOG else None


# ------------------------------------------------------------------ prices

_price_provider = None


def register_price_provider(func):
    """
    For orvahr_billing: ``func(plans) -> {plan_code: text}`` where text is
    what the public plan table shows (e.g. "฿1,490 / month"). Plans missing
    from the result show "Contact us". Call it from the billing AppConfig's
    ready().
    """
    global _price_provider
    _price_provider = func
    return func


def _billing_monthly_prices(plans):
    """Fallback: today's monthly ``orvahr_billing.PlanPrice`` of each plan."""
    from django.apps import apps
    from django.utils import timezone
    from django.utils.formats import number_format
    from django.utils.translation import gettext as _

    if not apps.is_installed("orvahr_billing"):
        return {}
    try:
        price_model = apps.get_model("orvahr_billing", "PlanPrice")
    except LookupError:
        return {}
    today = timezone.localdate()
    prices = {}
    for plan in plans:
        price = price_model.current(plan.code, "monthly", today)
        if price is None:
            continue
        parts = []
        if price.base_price:
            parts.append(
                _("฿%(amount)s / month")
                % {"amount": number_format(price.base_price, 0, force_grouping=True)}
            )
        if price.per_employee_price:
            parts.append(
                _("฿%(amount)s per employee / month")
                % {
                    "amount": number_format(
                        price.per_employee_price, 0, force_grouping=True
                    )
                }
            )
        if parts:
            prices[plan.code] = " + ".join(parts)
    return prices


def plan_prices(plans):
    """
    ``{plan_code: price text}`` for the public site; plans missing from it
    show "Contact us". Source, in order: a provider registered by billing
    (``register_price_provider``), ``orvahr_billing.pricing.public_prices``,
    then today's monthly ``orvahr_billing.PlanPrice`` rows.
    """
    plans = list(plans)
    provider = _price_provider
    if provider is None:
        try:
            provider = getattr(import_module("orvahr_billing.pricing"), "public_prices")
        except (ImportError, AttributeError):
            provider = _billing_monthly_prices
    try:
        return dict(provider(plans) or {})
    except Exception:  # noqa: BLE001 - prices are decoration, never a 500
        logger.exception("orvahr_billing price provider failed")
        return {}
