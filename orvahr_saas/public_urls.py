"""
URLconf for the public schema (base domain, unknown-host probes).

Deliberately tiny: health probes and a placeholder landing page. Tenant
routes (login, employees, ...) are NOT here -- they only exist on a tenant
host, because their tables only exist in tenant schemas. The control plane
(sign-up, billing, tenant admin) lands here in a later phase.
"""

from django.urls import path

from horilla.urls import health_check, readiness_check
from orvahr_saas import views

urlpatterns = [
    path("", views.landing, name="saas-landing"),
    path("health/", health_check),
    path("ready/", readiness_check),
]

# Django's default handlers render 404.html/500.html with the request context,
# whose context processors query tenant tables the public schema lacks.
handler404 = "orvahr_saas.views.not_found"
handler500 = "orvahr_saas.views.server_error"
