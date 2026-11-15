"""
Operator console at /console/ on the public host.

Who: active, staff ``ConsoleOperator`` rows only (``manage.py
create_console_operator``) -- not tenant users, which live in the tenant
schemas. The session holds the operator id plus a hash of the password hash,
so changing a password (or deactivating the operator) ends every session.
Failed logins are throttled per client IP and per username.

What it shows about a tenant: name, status, plan, domains and counts --
never an employee's personal data.
"""

import functools

from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from horilla.client_ip import client_ip
from horilla.tenancy import public_schema_name
from orvahr_saas import services
from orvahr_saas.forms import (
    ConsoleLoginForm,
    PlanChangeForm,
    ProvisionForm,
    SuspendForm,
)
from orvahr_saas.models import ConsoleOperator, OperatorAction, Tenant, TenantRequest
from orvahr_saas.operators import operator_password_problems
from orvahr_saas.views import render_public

SESSION_KEY = "orvahr_console_operator"
SESSION_HASH_KEY = "orvahr_console_operator_hash"
LOGIN_FAILURES = 5
LOGIN_LOCK_SECONDS = 15 * 60


def _session_hash(operator):
    return salted_hmac(
        "orvahr_saas.console.session", operator.password, algorithm="sha256"
    ).hexdigest()


def current_operator(request):
    pk = request.session.get(SESSION_KEY)
    if not pk:
        return None
    operator = ConsoleOperator.objects.filter(
        pk=pk, is_active=True, is_staff=True
    ).first()
    if operator is None or not constant_time_compare(
        request.session.get(SESSION_HASH_KEY, ""), _session_hash(operator)
    ):
        request.session.pop(SESSION_KEY, None)
        request.session.pop(SESSION_HASH_KEY, None)
        return None
    return operator


def on_tenant_host(request):
    tenant = getattr(request, "tenant", None)
    return tenant is not None and tenant.schema_name != public_schema_name()


def operator_required(view):
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        if on_tenant_host(request):
            raise Http404  # never on a tenant host
        operator = current_operator(request)
        if operator is None:
            return HttpResponseRedirect(
                reverse("saas-console-login") + "?next=" + request.path
            )
        request.operator = operator
        response = view(request, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        response["X-Robots-Tag"] = "noindex"
        return response

    return wrapper


def render_console(request, template, context=None, status=200):
    from django.contrib.messages import get_messages

    ctx = {
        "operator": getattr(request, "operator", None),
        "messages": list(get_messages(request)),
    }
    ctx.update(context or {})
    response = render_public(request, template, ctx, status=status)
    response["Cache-Control"] = "no-store"
    return response


def _ip(request):
    return client_ip(request)


# -------------------------------------------------------------------- login


def _lock_keys(request, username):
    return (
        f"orvahr_saas:console-fail:ip:{_ip(request)}",
        f"orvahr_saas:console-fail:user:{(username or '').lower()[:150]}",
    )


def _locked(keys):
    return any((cache.get(k) or 0) >= LOGIN_FAILURES for k in keys)


def _fail(keys):
    for key in keys:
        if not cache.add(key, 1, LOGIN_LOCK_SECONDS):
            try:
                cache.incr(key)
            except ValueError:
                cache.set(key, 1, LOGIN_LOCK_SECONDS)


@require_http_methods(["GET", "POST"])
def login_view(request):
    if on_tenant_host(request):
        raise Http404
    form = ConsoleLoginForm(request.POST or None)
    error = None
    next_url = request.POST.get("next") or request.GET.get("next") or ""
    if request.method == "POST" and form.is_valid():
        username = form.cleaned_data["username"].strip()
        keys = _lock_keys(request, username)
        if _locked(keys):
            error = _("Too many failed attempts. Try again in 15 minutes.")
        else:
            operator = ConsoleOperator.objects.filter(username=username).first()
            password = form.cleaned_data["password"]
            if operator is None:
                # Same cost as a real check, so timing does not reveal names.
                ConsoleOperator(password="").set_password(password)
                ok = False
            else:
                ok = operator.check_password(password)
            if not ok or not operator.is_active or not operator.is_staff:
                _fail(keys)
                error = _("Invalid username or password.")
            elif operator_password_problems(password, operator):
                error = _(
                    "Your password no longer meets the password policy. Ask "
                    "an administrator to reset it with create_console_operator."
                )
            else:
                for key in keys:
                    cache.delete(key)
                request.session.cycle_key()
                request.session[SESSION_KEY] = operator.pk
                request.session[SESSION_HASH_KEY] = _session_hash(operator)
                operator.last_login = timezone.now()
                operator.save(update_fields=["last_login"])
                services.record_action(
                    "login", operator=operator, ip_address=_ip(request)
                )
                if not url_has_allowed_host_and_scheme(
                    next_url, allowed_hosts={request.get_host()}
                ) or not next_url.startswith("/console/"):
                    next_url = reverse("saas-console")
                return HttpResponseRedirect(next_url)
    return render_console(
        request,
        "orvahr_saas/console/login.html",
        {"form": form, "error": error, "next": next_url},
        status=200 if error is None else 403,
    )


@require_POST
def logout_view(request):
    operator = current_operator(request)
    if operator is not None:
        services.record_action("logout", operator=operator, ip_address=_ip(request))
    request.session.flush()
    return HttpResponseRedirect(reverse("saas-console-login"))


# ------------------------------------------------------------------ tenants


@operator_required
def tenant_list(request):
    tenants = Tenant.objects.exclude(schema_name="public").select_related("plan")
    status = request.GET.get("status")
    if status:
        tenants = tenants.filter(status=status)
    refresh = request.GET.get("refresh") == "1"
    rows = []
    for tenant in tenants:
        usage = services.tenant_usage(tenant, refresh=refresh)
        rows.append(
            {
                "tenant": tenant,
                "usage": usage,
                "limit": tenant.employee_limit,
                "domain": next(
                    (d.domain for d in tenant.domains.all() if d.is_primary), ""
                ),
            }
        )
    return render_console(
        request,
        "orvahr_saas/console/tenants.html",
        {
            "rows": rows,
            "status": status or "",
            "statuses": Tenant.STATUS_CHOICES,
            "pending_requests": TenantRequest.objects.filter(
                status=TenantRequest.STATUS_PENDING
            ).count(),
        },
    )


@operator_required
def tenant_detail(request, pk):
    tenant = get_object_or_404(Tenant.objects.exclude(schema_name="public"), pk=pk)
    return render_console(
        request,
        "orvahr_saas/console/tenant_detail.html",
        {
            "tenant": tenant,
            "domains": tenant.domains.order_by("-is_primary", "domain"),
            "usage": services.tenant_usage(tenant),
            "plan_form": PlanChangeForm(
                initial={"plan": tenant.plan, "max_employees": tenant.max_employees}
            ),
            "suspend_form": SuspendForm(),
            "actions": OperatorAction.objects.filter(tenant=tenant).select_related(
                "operator"
            )[:50],
        },
    )


def _tenant_or_404(pk):
    return get_object_or_404(Tenant.objects.exclude(schema_name="public"), pk=pk)


@require_POST
@operator_required
def tenant_suspend(request, pk):
    tenant = _tenant_or_404(pk)
    form = SuspendForm(request.POST)
    if form.is_valid():
        services.suspend_tenant(
            tenant,
            form.cleaned_data["reason"],
            operator=request.operator,
            ip_address=_ip(request),
        )
        messages.success(request, _("Tenant suspended."))
    else:
        messages.error(request, _("A reason is required to suspend a tenant."))
    return HttpResponseRedirect(reverse("saas-console-tenant", args=[pk]))


@require_POST
@operator_required
def tenant_resume(request, pk):
    tenant = _tenant_or_404(pk)
    services.resume_tenant(tenant, operator=request.operator, ip_address=_ip(request))
    messages.success(request, _("Tenant resumed."))
    return HttpResponseRedirect(reverse("saas-console-tenant", args=[pk]))


@require_POST
@operator_required
def tenant_change_plan(request, pk):
    tenant = _tenant_or_404(pk)
    form = PlanChangeForm(request.POST)
    if form.is_valid():
        services.change_plan(
            tenant,
            form.cleaned_data["plan"],
            max_employees=form.cleaned_data["max_employees"],
            operator=request.operator,
            ip_address=_ip(request),
        )
        messages.success(request, _("Plan changed."))
    else:
        messages.error(request, _("Please choose a valid plan."))
    return HttpResponseRedirect(reverse("saas-console-tenant", args=[pk]))


# ----------------------------------------------------------------- requests


@operator_required
def request_list(request):
    status = request.GET.get("status", TenantRequest.STATUS_PENDING)
    qs = TenantRequest.objects.select_related("plan")
    if status != "all":
        qs = qs.filter(status=status)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render_console(
        request,
        "orvahr_saas/console/requests.html",
        {"page": page, "status": status, "statuses": TenantRequest.STATUS_CHOICES},
    )


@operator_required
def request_detail(request, pk):
    tenant_request = get_object_or_404(TenantRequest, pk=pk)
    form = None
    if tenant_request.status == TenantRequest.STATUS_APPROVED:
        form = ProvisionForm(
            tenant_request=tenant_request,
            initial={
                "domain": services.default_domain_for(tenant_request.slug),
                "plan": tenant_request.plan,
                "status": Tenant.STATUS_TRIAL,
                "thai_setup": True,
            },
        )
    return _render_request(request, tenant_request, form)


def _render_request(request, tenant_request, form, status=200):
    available, availability = (None, "")
    if tenant_request.status in (
        TenantRequest.STATUS_PENDING,
        TenantRequest.STATUS_APPROVED,
    ):
        # The request itself holds the slug; check everything else.
        available, availability = _slug_free_apart_from(tenant_request)
    return render_console(
        request,
        "orvahr_saas/console/request_detail.html",
        {
            "req": tenant_request,
            "form": form,
            "available": available,
            "availability": availability,
            "running": tenant_request.status == TenantRequest.STATUS_PROVISIONING,
        },
        status=status,
    )


def _slug_free_apart_from(tenant_request):
    from django_tenants.utils import schema_exists

    from orvahr_saas.models import schema_name_for_slug

    slug = tenant_request.slug
    schema = schema_name_for_slug(slug)
    if (
        Tenant.objects.filter(slug=slug).exists()
        or Tenant.objects.filter(schema_name=schema).exists()
        or schema_exists(schema)
    ):
        return False, _("A tenant with this name already exists.")
    other = (
        TenantRequest.objects.filter(slug=slug, status__in=TenantRequest.HOLDS_SLUG)
        .exclude(pk=tenant_request.pk)
        .exists()
    )
    if other:
        return False, _("Another open request holds this name.")
    return True, _("Available")


def _transition(request, pk, allowed, new_status, action):
    tenant_request = get_object_or_404(TenantRequest, pk=pk)
    if tenant_request.status not in allowed:
        messages.error(
            request, _("This request cannot be changed in its current state.")
        )
    else:
        tenant_request.status = new_status
        tenant_request.reviewed_by = request.operator
        tenant_request.reviewed_at = timezone.now()
        tenant_request.save(update_fields=["status", "reviewed_by", "reviewed_at"])
        services.record_action(
            action,
            operator=request.operator,
            tenant_request=tenant_request,
            ip_address=_ip(request),
            slug=tenant_request.slug,
        )
        messages.success(request, _("Request updated."))
    return HttpResponseRedirect(reverse("saas-console-request", args=[pk]))


@require_POST
@operator_required
def request_approve(request, pk):
    return _transition(
        request,
        pk,
        (TenantRequest.STATUS_PENDING, TenantRequest.STATUS_FAILED),
        TenantRequest.STATUS_APPROVED,
        "approve_request",
    )


@require_POST
@operator_required
def request_reject(request, pk):
    return _transition(
        request,
        pk,
        (
            TenantRequest.STATUS_PENDING,
            TenantRequest.STATUS_APPROVED,
            TenantRequest.STATUS_FAILED,
        ),
        TenantRequest.STATUS_REJECTED,
        "reject_request",
    )


@require_POST
@operator_required
def request_provision(request, pk):
    tenant_request = get_object_or_404(TenantRequest, pk=pk)
    if tenant_request.status != TenantRequest.STATUS_APPROVED:
        messages.error(request, _("Approve the request before creating the tenant."))
        return HttpResponseRedirect(reverse("saas-console-request", args=[pk]))
    form = ProvisionForm(request.POST, tenant_request=tenant_request)
    available, availability = _slug_free_apart_from(tenant_request)
    if not available:
        form.add_error(None, availability)
    if not form.is_valid():
        return _render_request(request, tenant_request, form, status=400)
    try:
        services.start_provisioning(
            tenant_request,
            admin_password=form.cleaned_data["admin_password"],
            domain=form.cleaned_data["domain"],
            plan=form.cleaned_data["plan"],
            status=form.cleaned_data["status"],
            thai_setup=form.cleaned_data["thai_setup"],
            operator=request.operator,
            ip_address=_ip(request),
        )
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    else:
        messages.success(
            request,
            _("Provisioning started. This takes a few minutes; this page refreshes."),
        )
    return HttpResponseRedirect(reverse("saas-console-request", args=[pk]))


@operator_required
def request_status(request, pk):
    tenant_request = get_object_or_404(TenantRequest, pk=pk)
    return JsonResponse(
        {
            "status": tenant_request.status,
            "log": tenant_request.provision_log[-4000:],
            "tenant": tenant_request.tenant_id,
        }
    )


# -------------------------------------------------------------------- audit


@operator_required
def audit_log(request):
    qs = OperatorAction.objects.select_related("operator", "tenant", "tenant_request")
    tenant = request.GET.get("tenant")
    if tenant and tenant.isdigit():
        qs = qs.filter(tenant_id=int(tenant))
    page = Paginator(qs, 100).get_page(request.GET.get("page"))
    return render_console(request, "orvahr_saas/console/audit.html", {"page": page})
