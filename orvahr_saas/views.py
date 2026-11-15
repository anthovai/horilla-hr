"""
Public (control-plane) site: landing page with plans, trial sign-up, the
sub-domain availability check, and the error handlers of the public urlconf.

Pages are rendered WITHOUT the request context on purpose: the project's
context processors query tenant tables the public schema does not have. The
CSRF token is passed in explicitly.
"""

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import redirect
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.translation import get_language
from django.views.decorators.http import require_GET, require_http_methods

from horilla.client_ip import client_ip

SIGNUP_LIMIT = (5, 60 * 60)  # POSTs per IP per hour
CHECK_LIMIT = (60, 60)  # availability checks per IP per minute
FORM_MIN_SECONDS = 3  # a human needs longer than this to fill the form
_FORM_SALT = "orvahr_saas.signup.form-ts"


def render_public(request, template, context=None, status=200):
    ctx = {
        "csrf_token": get_token(request),
        "LANGUAGE_CODE": get_language() or settings.LANGUAGE_CODE,
        "base_domain": getattr(settings, "ORVAHR_SAAS_BASE_DOMAIN", ""),
        "request_path": request.get_full_path(),
    }
    ctx.update(context or {})
    return HttpResponse(render_to_string(template, ctx), status=status)


def rate_limited(request, scope, limit, window):
    """Count one hit for this client in ``scope``; True once over ``limit``."""
    key = f"orvahr_saas:rl:{scope}:{client_ip(request)}"
    if cache.add(key, 1, window):
        return False
    try:
        hits = cache.incr(key)
    except ValueError:  # expired between add() and incr()
        cache.set(key, 1, window)
        return False
    return hits > limit


def _plans_with_prices():
    from orvahr_saas.models import Plan
    from orvahr_saas.plans import MODULE_CATALOG, plan_prices

    plans = list(Plan.objects.filter(is_public=True))
    prices = plan_prices(plans)
    rows = []
    for plan in plans:
        rows.append(
            {
                "plan": plan,
                "price": prices.get(plan.code),
                "modules": [
                    str(name)
                    for label, name in MODULE_CATALOG.items()
                    if plan.has_module(label)
                ],
            }
        )
    return rows


def landing(request):
    return render_public(
        request, "orvahr_saas/public/landing.html", {"plan_rows": _plans_with_prices()}
    )


@require_http_methods(["GET", "POST"])
def signup(request):
    from orvahr_saas.forms import SignupForm
    from orvahr_saas.models import Plan

    form_ts = signing.TimestampSigner(salt=_FORM_SALT).sign(
        str(int(timezone.now().timestamp()))
    )
    if request.method == "POST":
        if rate_limited(request, "signup", *SIGNUP_LIMIT):
            return render_public(
                request, "orvahr_saas/public/rate_limited.html", status=429
            )
        form = SignupForm(request.POST)
        too_fast = not _form_age_ok(request.POST.get("ts", ""))
        if form.is_valid() and not too_fast:
            obj = form.save(commit=False)
            obj.consent_at = timezone.now()
            obj.language = (get_language() or "")[:10]
            obj.ip_address = client_ip(request)
            obj.save()
            from orvahr_saas import notifications

            notifications.request_received(obj)
            return redirect("saas-signup-thanks")
        if form.is_spam or too_fast:
            # Look like success to a bot; store nothing.
            return redirect("saas-signup-thanks")
    else:
        initial = {}
        code = request.GET.get("plan")
        if code:
            initial["plan"] = Plan.objects.filter(code=code, is_public=True).first()
        form = SignupForm(initial=initial)
    return render_public(
        request, "orvahr_saas/public/signup.html", {"form": form, "form_ts": form_ts}
    )


def _form_age_ok(value):
    try:
        issued = signing.TimestampSigner(salt=_FORM_SALT).unsign(
            value, max_age=60 * 60 * 6
        )
        return timezone.now().timestamp() - int(issued) >= FORM_MIN_SECONDS
    except (signing.BadSignature, ValueError):
        return False


def signup_thanks(request):
    return render_public(request, "orvahr_saas/public/thanks.html")


@require_GET
def check_slug(request):
    from orvahr_saas.services import default_domain_for, slug_availability

    if rate_limited(request, "check", *CHECK_LIMIT):
        return JsonResponse(
            {"available": False, "message": "Too many requests"}, status=429
        )
    slug = (request.GET.get("slug") or "").strip().lower()[:63]
    available, message = slug_availability(slug)
    return JsonResponse(
        {
            "slug": slug,
            "available": available,
            "message": str(message),
            "domain": default_domain_for(slug) if available else "",
        }
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
