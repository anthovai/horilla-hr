"""
URLconf for the public schema (base domain, unknown-host probes).

The control plane: landing page with plans, trial sign-up, the operator
console (/console/), billing and health probes. Tenant routes (login,
employees, ...) are NOT here -- they only exist on a tenant host, because
their tables only exist in tenant schemas.
"""

from django.urls import include, path

from horilla.urls import health_check, readiness_check
from orvahr_saas import console, views

console_patterns = [
    path("", console.tenant_list, name="saas-console"),
    path("login/", console.login_view, name="saas-console-login"),
    path("logout/", console.logout_view, name="saas-console-logout"),
    path("tenants/<int:pk>/", console.tenant_detail, name="saas-console-tenant"),
    path(
        "tenants/<int:pk>/suspend/",
        console.tenant_suspend,
        name="saas-console-tenant-suspend",
    ),
    path(
        "tenants/<int:pk>/resume/",
        console.tenant_resume,
        name="saas-console-tenant-resume",
    ),
    path(
        "tenants/<int:pk>/plan/",
        console.tenant_change_plan,
        name="saas-console-tenant-plan",
    ),
    path("requests/", console.request_list, name="saas-console-requests"),
    path("requests/<int:pk>/", console.request_detail, name="saas-console-request"),
    path(
        "requests/<int:pk>/approve/",
        console.request_approve,
        name="saas-console-request-approve",
    ),
    path(
        "requests/<int:pk>/reject/",
        console.request_reject,
        name="saas-console-request-reject",
    ),
    path(
        "requests/<int:pk>/provision/",
        console.request_provision,
        name="saas-console-request-provision",
    ),
    path(
        "requests/<int:pk>/status/",
        console.request_status,
        name="saas-console-request-status",
    ),
    path("audit/", console.audit_log, name="saas-console-audit"),
]

urlpatterns = [
    path("", views.landing, name="saas-landing"),
    path("signup/", views.signup, name="saas-signup"),
    path("signup/thanks/", views.signup_thanks, name="saas-signup-thanks"),
    path("signup/check/", views.check_slug, name="saas-signup-check"),
    path("console/", include(console_patterns)),
    path("i18n/", include("django.conf.urls.i18n")),
    path("health/", health_check),
    path("ready/", readiness_check),
    path("billing/", include("orvahr_billing.urls")),
]

# Django's default handlers render 404.html/500.html with the request context,
# whose context processors query tenant tables the public schema lacks.
handler404 = "orvahr_saas.views.not_found"
handler500 = "orvahr_saas.views.server_error"
