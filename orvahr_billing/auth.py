"""
Who may use the billing UI: active staff operators of the control-plane
console (``orvahr_saas.ConsoleOperator``, signed in at /console/login/).

Tenant users can never reach it: their user table lives in tenant schemas,
and the billing URLs exist only in the public urlconf (a tenant host gets a
404 here as well). A project can supply another check with the setting
``ORVAHR_BILLING_OPERATOR_RESOLVER`` = dotted path of ``f(request) ->
operator or None``. With neither available, every request is refused.
"""

import functools
import logging

from django.conf import settings
from django.http import Http404, HttpResponseForbidden, HttpResponseRedirect
from django.urls import NoReverseMatch, reverse
from django.utils.http import urlencode
from django.utils.module_loading import import_string

from horilla.tenancy import public_schema_name

logger = logging.getLogger(__name__)


def _resolver():
    dotted = getattr(settings, "ORVAHR_BILLING_OPERATOR_RESOLVER", None)
    if dotted:
        return import_string(dotted)
    try:
        from orvahr_saas.console import current_operator
    except ImportError:
        return None
    return current_operator


def get_operator(request):
    resolver = _resolver()
    if resolver is None:
        return None
    operator = resolver(request)
    if operator is None:
        return None
    if not getattr(operator, "is_active", False) or not getattr(
        operator, "is_staff", False
    ):
        return None
    return operator


def operator_label(request):
    operator = getattr(request, "operator", None)
    return getattr(operator, "username", "") or str(operator or "")


def _on_tenant_host(request):
    tenant = getattr(request, "tenant", None)
    return (
        tenant is not None
        and getattr(tenant, "schema_name", None) != public_schema_name()
    )


def login_url(next_path):
    try:
        url = reverse("saas-console-login")
    except NoReverseMatch:
        return None
    return f"{url}?{urlencode({'next': next_path})}"


def operator_required(view):
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        if _on_tenant_host(request):
            raise Http404
        operator = get_operator(request)
        if operator is None:
            url = login_url(request.get_full_path()) if _resolver() else None
            if url and request.method == "GET":
                return HttpResponseRedirect(url)
            return HttpResponseForbidden("Billing operators only.")
        request.operator = operator
        response = view(request, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        response["X-Robots-Tag"] = "noindex"
        return response

    return wrapper
