"""
Host -> tenant routing (first middleware in SaaS mode).

Extends django-tenants' TenantMainMiddleware with three OrvaHR rules:

* hosts in ORVAHR_SAAS_PUBLIC_HOSTS (the bare base domain, www.) and the
  liveness/readiness probes on any unknown host get the public urlconf, so a
  load balancer probing by IP still sees /health/;
* any other unknown host gets a plain 404 page instead of a debug traceback;
* a suspended tenant gets a friendly 403 page and its schema is never
  activated, so nothing below this middleware can read its data;
* a tenant whose trial ended (``trial_expired``) still serves the paths in
  ORVAHR_SAAS_ALLOWED_WHEN_LOCKED (sign-in/out, the /subscription/ pages, static
  and media files) so an administrator can sign in and subscribe; every
  other path gets a friendly "trial ended" page (JSON 403 under /api/).

The pages are rendered without a request context on purpose: the context
processors query tenant tables (companies, menus...) that the public schema
does not have.
"""

from django.conf import settings
from django.db import connection
from django.http import HttpResponseForbidden, HttpResponseNotFound
from django.template.loader import render_to_string
from django.utils.translation import get_language
from django_tenants.middleware.main import TenantMainMiddleware

PROBE_PATHS = ("/health/", "/ready/")


class TenantMiddleware(TenantMainMiddleware):
    def process_request(self, request):
        response = super().process_request(request)
        if response is not None:
            return response
        tenant = getattr(request, "tenant", None)
        if tenant is not None and getattr(tenant, "is_suspended", False):
            connection.set_schema_to_public()
            return HttpResponseForbidden(
                render_to_string(
                    "orvahr_saas/suspended.html", {"tenant_name": tenant.name}
                )
            )
        if (
            tenant is not None
            and getattr(tenant, "is_trial_expired", False)
            and not allowed_when_locked(request.path_info)
        ):
            connection.set_schema_to_public()
            return trial_expired_response(request, tenant)
        return None

    def no_tenant_found(self, request, hostname):
        public_hosts = getattr(settings, "ORVAHR_SAAS_PUBLIC_HOSTS", [])
        if hostname.lower() in public_hosts or request.path in PROBE_PATHS:
            request.tenant = None
            self.setup_url_routing(request=request, force_public=True)
            return None
        return HttpResponseNotFound(render_to_string("orvahr_saas/not_found.html"))


def allowed_when_locked(path):
    """True if a trial-expired tenant still serves ``path``."""
    from orvahr_saas.conf import allowed_when_locked as prefixes

    if path in PROBE_PATHS:
        return True
    for prefix in prefixes():
        if not prefix:
            continue
        # "/login/" also covers "/login" (settings.LOGIN_URL has no slash).
        if path.startswith(prefix) or (
            prefix.endswith("/") and path == prefix.rstrip("/")
        ):
            return True
    return False


def trial_expired_response(request, tenant):
    from orvahr_saas.conf import support_email

    if _is_api(request):
        from django.http import JsonResponse

        return JsonResponse(
            {
                "detail": "The trial of this organisation has ended.",
                "code": "trial_expired",
            },
            status=403,
        )
    return HttpResponseForbidden(
        render_to_string(
            "orvahr_saas/trial_expired.html",
            {
                "tenant_name": tenant.name,
                "support_email": support_email(),
                "account_url": "/subscription/",
                "login_url": "/login/",
                "logout_url": "/logout/",
            },
        )
    )


def _is_api(request):
    return request.path_info.startswith("/api/")


class PlanGateMiddleware:
    """
    Plan enforcement inside a tenant (SaaS mode only; listed right after
    TenantMiddleware):

    * a URL of an optional module the tenant's plan lacks (``/payroll/...``,
      ``/api/v1/payroll/...``, or a view living in that app) answers 403 with
      a friendly page (JSON for the API) instead of running the view;
    * ``EmployeeLimitReached`` escaping a view (an ``Employee.save()`` outside
      a form: REST API, onboarding conversion, un-archive, import) becomes a
      403 JSON answer for the API and a "back where you were" redirect with an
      error message for the web UI.

    The public host (``request.tenant is None``) and tenants without a plan
    pass straight through.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        tenant = getattr(request, "tenant", None)
        if tenant is None or not getattr(tenant, "plan_id", None):
            return None
        from orvahr_saas.plans import (
            MODULE_CATALOG,
            module_enabled,
            module_for_path,
            module_for_view,
        )

        label = module_for_path(request.path_info) or module_for_view(view_func)
        if label is None or module_enabled(label, tenant):
            return None
        module_name = str(MODULE_CATALOG[label])
        if _is_api(request):
            from django.http import JsonResponse
            from django.utils.translation import gettext as _

            return JsonResponse(
                {
                    "detail": _("%(module)s is not included in your plan.")
                    % {"module": module_name},
                    "code": "module_not_in_plan",
                    "module": label,
                },
                status=403,
            )
        return HttpResponseForbidden(
            render_to_string(
                "orvahr_saas/module_disabled.html",
                {
                    "module_name": module_name,
                    "fragment": bool(request.headers.get("HX-Request")),
                    "LANGUAGE_CODE": get_language(),
                },
            )
        )

    def process_exception(self, request, exception):
        from orvahr_saas.limits import is_limit_error, limit_error_text

        if getattr(request, "tenant", None) is None or not is_limit_error(exception):
            return None
        message = limit_error_text(exception)
        if _is_api(request):
            from django.http import JsonResponse

            return JsonResponse(
                {"detail": message, "code": "employee_limit_reached"}, status=403
            )
        from horilla.http.response import HorillaRedirect

        return HorillaRedirect(request, message=message)
