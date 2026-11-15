"""
Host -> tenant routing (first middleware in SaaS mode).

Extends django-tenants' TenantMainMiddleware with three OrvaHR rules:

* hosts in ORVAHR_SAAS_PUBLIC_HOSTS (the bare base domain, www.) and the
  liveness/readiness probes on any unknown host get the public urlconf, so a
  load balancer probing by IP still sees /health/;
* any other unknown host gets a plain 404 page instead of a debug traceback;
* a suspended tenant gets a friendly 403 page and its schema is never
  activated, so nothing below this middleware can read its data.

The pages are rendered without a request context on purpose: the context
processors query tenant tables (companies, menus...) that the public schema
does not have.
"""

from django.conf import settings
from django.db import connection
from django.http import HttpResponseForbidden, HttpResponseNotFound
from django.template.loader import render_to_string
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
        return None

    def no_tenant_found(self, request, hostname):
        public_hosts = getattr(settings, "ORVAHR_SAAS_PUBLIC_HOSTS", [])
        if hostname.lower() in public_hosts or request.path in PROBE_PATHS:
            request.tenant = None
            self.setup_url_routing(request=request, force_public=True)
            return None
        return HttpResponseNotFound(render_to_string("orvahr_saas/not_found.html"))
