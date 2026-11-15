from django.conf import settings
from django.http import HttpResponse
from django.template.loader import render_to_string


def landing(request):
    """Placeholder public landing page (no request context: see middleware)."""
    return HttpResponse(
        render_to_string(
            "orvahr_saas/landing.html",
            {"base_domain": getattr(settings, "ORVAHR_SAAS_BASE_DOMAIN", "")},
        )
    )


def not_found(request, exception=None):
    """404 on the public schema: no tenant context processors (no tables)."""
    from django.http import HttpResponseNotFound

    return HttpResponseNotFound(render_to_string("orvahr_saas/not_found.html"))


def server_error(request):
    from django.http import HttpResponseServerError

    return HttpResponseServerError(
        "<h1>Server error</h1>", content_type="text/html; charset=utf-8"
    )
