"""
Lazy hooks into the control plane (orvahr_saas), which is developed
separately. Every lookup is guarded: if the control plane does not (yet)
provide a model or function, billing degrades instead of failing.

* ``get_plan(code)``       -> ``orvahr_saas.Plan`` with that code, or None.
* ``get_suspend_tenant()`` -> ``suspend_tenant(tenant, reason)`` or None.
* ``get_resume_tenant()``  -> ``resume_tenant(tenant)`` or None.

The functions are looked up, in order, at the dotted path in the settings
``ORVAHR_BILLING_SUSPEND_FUNC`` / ``ORVAHR_BILLING_RESUME_FUNC``, then in
``orvahr_saas.services``, ``orvahr_saas.service``, ``orvahr_saas.lifecycle``
and ``orvahr_saas.utils``.
"""

import importlib
import logging

from django.conf import settings
from django.utils.module_loading import import_string

logger = logging.getLogger(__name__)

_CANDIDATE_MODULES = (
    "orvahr_saas.services",
    "orvahr_saas.service",
    "orvahr_saas.lifecycle",
    "orvahr_saas.utils",
)


def get_plan_model():
    from django.apps import apps

    try:
        return apps.get_model("orvahr_saas", "Plan")
    except LookupError:
        return None


def get_plan(code):
    model = get_plan_model()
    if model is None or not code:
        return None
    try:
        return model.objects.filter(code=code).first()
    except Exception:  # noqa: BLE001 - table not migrated yet, field renamed...
        logger.warning("orvahr_saas.Plan lookup failed for %r", code, exc_info=True)
        return None


def plan_name(code):
    plan = get_plan(code)
    return getattr(plan, "name", None) or code


def plan_choices():
    """[(code, label)] from orvahr_saas.Plan when it exists, else []."""
    model = get_plan_model()
    if model is None:
        return []
    try:
        return [(p.code, f"{p.name} ({p.code})") for p in model.objects.all()]
    except Exception:  # noqa: BLE001
        return []


def _lookup(setting_name, func_name):
    dotted = getattr(settings, setting_name, None)
    if dotted:
        try:
            return import_string(dotted)
        except ImportError:
            logger.error("%s=%r cannot be imported", setting_name, dotted)
            return None
    for module_name in _CANDIDATE_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        func = getattr(module, func_name, None)
        if callable(func):
            return func
    return None


def get_suspend_tenant():
    return _lookup("ORVAHR_BILLING_SUSPEND_FUNC", "suspend_tenant")


def get_resume_tenant():
    return _lookup("ORVAHR_BILLING_RESUME_FUNC", "resume_tenant")


def notify(event, obj):
    """Tell the control plane about a billing event (e-mail); never raises."""
    try:
        from orvahr_saas.notifications import billing_event
    except ImportError:
        return
    try:
        billing_event(event, obj)
    except Exception:  # noqa: BLE001 - a notification must not undo billing
        logger.exception("orvahr_saas notification %s failed", event)
