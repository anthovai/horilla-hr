"""
Operator console at /console/ on the public host.

Who: active, staff ``ConsoleOperator`` rows only (``manage.py
create_console_operator``) -- not tenant users, which live in the tenant
schemas. The session holds the operator id plus a hash of the password hash
and TOTP secret, so changing a password, resetting 2FA (or deactivating the
operator) ends every session. Failed logins are throttled per client IP and
per username.

Two-factor (orvahr_saas.twofactor): an operator with TOTP is only signed in
after the second step (/console/login/verify/, code or recovery code). While
2FA is mandatory (ORVAHR_CONSOLE_REQUIRE_2FA, from
ORVAHR_CONSOLE_REQUIRE_2FA_FROM) an operator without it can only reach the
enrolment page (/console/2fa/) after the password step. ``current_operator``
-- which billing uses too -- returns only fully signed-in operators.

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
from orvahr_saas import conf, services, totp, twofactor
from orvahr_saas.forms import (
    ConsoleLoginForm,
    ExtendTrialForm,
    PlanChangeForm,
    PlanRequestDecisionForm,
    ProvisionForm,
    SuspendForm,
    TwoFactorCodeForm,
)
from orvahr_saas.models import ConsoleOperator, OperatorAction, Tenant, TenantRequest
from orvahr_saas.operators import operator_password_problems
from orvahr_saas.views import render_public

SESSION_KEY = "orvahr_console_operator"
SESSION_HASH_KEY = "orvahr_console_operator_hash"
# True once the second factor was checked in this session.
SESSION_MFA_KEY = "orvahr_console_mfa"
# When the password step succeeded (the second step must follow quickly).
SESSION_AUTH_AT_KEY = "orvahr_console_auth_at"
# Secret shown on the enrolment page, waiting for the confirming code.
SESSION_PENDING_SECRET_KEY = "orvahr_console_totp_pending"
LOGIN_FAILURES = 5
LOGIN_LOCK_SECONDS = 15 * 60
SECOND_STEP_SECONDS = 5 * 60
ENROL_SECONDS = 15 * 60  # time to install an app and confirm the first code


def _session_hash(operator):
    material = f"{operator.password}|{operator.totp_secret or ''}"
    return salted_hmac(
        "orvahr_saas.console.session", material, algorithm="sha256"
    ).hexdigest()


def current_operator(request):
    """
    The fully signed-in operator, or None: password step done and -- when the
    operator has TOTP or 2FA is mandatory -- the second step too.
    """
    operator = session_operator(request)
    if operator is None:
        return None
    if operator.has_totp:
        return operator if request.session.get(SESSION_MFA_KEY) else None
    if conf.two_factor_required():
        return None  # must enrol first
    return operator


def session_operator(request):
    """The operator whose password step succeeded in this session (or None)."""
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
            pending = session_operator(request)
            if pending is not None and pending.has_totp:
                return HttpResponseRedirect(reverse("saas-console-login-verify"))
            if pending is not None:
                return HttpResponseRedirect(reverse("saas-console-2fa"))
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

    operator = getattr(request, "operator", None)
    ctx = {
        "operator": operator,
        "messages": list(get_messages(request)),
        "pending_plan_change_count": (
            _pending_plan_change_count() if operator is not None else 0
        ),
    }
    ctx.update(context or {})
    response = render_public(request, template, ctx, status=status)
    response["Cache-Control"] = "no-store"
    return response


def _pending_plan_change_count():
    """Nav badge: tenants' pending plan change requests (0 if unavailable)."""
    import logging

    try:
        from orvahr_account.services import pending_plan_change_request_count

        return pending_plan_change_request_count()
    except Exception:  # noqa: BLE001 - a badge must never break a page
        logging.getLogger(__name__).exception("Could not count plan requests")
        return 0


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
                request.session[SESSION_MFA_KEY] = False
                request.session[SESSION_AUTH_AT_KEY] = int(timezone.now().timestamp())
                request.session["orvahr_console_next"] = _safe_next(request, next_url)
                if operator.has_totp:
                    services.record_action(
                        "login_password_ok", operator=operator, ip_address=_ip(request)
                    )
                    return HttpResponseRedirect(reverse("saas-console-login-verify"))
                if conf.two_factor_required():
                    services.record_action(
                        "login_password_ok",
                        operator=operator,
                        ip_address=_ip(request),
                        enrolment_required=True,
                    )
                    return HttpResponseRedirect(reverse("saas-console-2fa"))
                return _complete_login(request, operator, method="password")
    return render_console(
        request,
        "orvahr_saas/console/login.html",
        {"form": form, "error": error, "next": next_url},
        status=200 if error is None else 403,
    )


def _safe_next(request, next_url):
    if not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}
    ) or not (next_url or "").startswith("/console/"):
        return reverse("saas-console")
    return next_url


def _complete_login(request, operator, *, method):
    """Final step of a sign-in: mark the session, audit, go where asked."""
    next_url = request.session.pop("orvahr_console_next", "") or reverse("saas-console")
    request.session.cycle_key()
    request.session[SESSION_MFA_KEY] = method != "password"
    request.session[SESSION_HASH_KEY] = _session_hash(operator)
    request.session.pop(SESSION_AUTH_AT_KEY, None)
    operator.last_login = timezone.now()
    operator.save(update_fields=["last_login"])
    services.record_action(
        "login", operator=operator, ip_address=_ip(request), method=method
    )
    if method == "password" and conf.require_2fa_enabled():
        start = conf.require_2fa_from()
        if start is not None:
            messages.warning(
                request,
                _(
                    "Two-factor authentication becomes mandatory on %(date)s. "
                    "Set it up now under Two-factor."
                )
                % {"date": start.isoformat()},
            )
    return HttpResponseRedirect(_safe_next(request, next_url))


def _second_step_expired(request, limit=SECOND_STEP_SECONDS):
    started = request.session.get(SESSION_AUTH_AT_KEY)
    if started is None:
        return False
    return timezone.now().timestamp() - started > limit


@require_http_methods(["GET", "POST"])
def login_verify(request):
    """Second sign-in step: TOTP code or a one-time recovery code."""
    if on_tenant_host(request):
        raise Http404
    operator = session_operator(request)
    if operator is None or not operator.has_totp:
        return HttpResponseRedirect(reverse("saas-console-login"))
    if request.session.get(SESSION_MFA_KEY):
        return HttpResponseRedirect(reverse("saas-console"))
    if _second_step_expired(request):
        request.session.flush()
        return HttpResponseRedirect(reverse("saas-console-login"))
    form = TwoFactorCodeForm(request.POST or None)
    error = None
    if twofactor.is_locked(operator):
        error = _("Too many wrong codes. Try again in 15 minutes.")
    elif request.method == "POST" and form.is_valid():
        ok, method, problem = twofactor.verify_second_factor(
            operator, form.cleaned_data["code"], ip_address=_ip(request)
        )
        if ok:
            if method == "recovery_code":
                messages.warning(
                    request,
                    _(
                        "You signed in with a recovery code. %(n)s left: "
                        "generate new ones under Two-factor."
                    )
                    % {"n": twofactor.remaining_recovery_codes(operator)},
                )
            return _complete_login(request, operator, method=method)
        if problem == "locked":
            error = _("Too many wrong codes. Try again in 15 minutes.")
        else:
            error = _("Invalid code.")
    return render_console(
        request,
        "orvahr_saas/console/login_verify.html",
        {"form": form, "error": error},
        status=200 if error is None else 403,
    )


@require_http_methods(["GET", "POST"])
def two_factor(request):
    """
    Enrol (password step done, no TOTP yet), or -- fully signed in --
    regenerate recovery codes / switch 2FA off when it is not mandatory.
    """
    if on_tenant_host(request):
        raise Http404
    operator = session_operator(request)
    if operator is None:
        return HttpResponseRedirect(reverse("saas-console-login"))
    if operator.has_totp and not request.session.get(SESSION_MFA_KEY):
        return HttpResponseRedirect(reverse("saas-console-login-verify"))
    if (
        not operator.has_totp
        and current_operator(request) is None
        and _second_step_expired(request, ENROL_SECONDS)
    ):
        request.session.flush()
        return HttpResponseRedirect(reverse("saas-console-login"))
    request.operator = operator if current_operator(request) else None
    form = TwoFactorCodeForm(request.POST or None)
    error = None
    codes = None
    action = request.POST.get("action", "")

    if not operator.has_totp:
        secret = request.session.get(SESSION_PENDING_SECRET_KEY) or totp.new_secret()
        request.session[SESSION_PENDING_SECRET_KEY] = secret
        if request.method == "POST" and form.is_valid():
            codes = twofactor.enrol(
                operator, secret, form.cleaned_data["code"], ip_address=_ip(request)
            )
            if codes is None:
                error = _("Invalid code. Check the time on your phone and try again.")
            else:
                request.session.pop(SESSION_PENDING_SECRET_KEY, None)
                was_signed_in = request.session.get(SESSION_MFA_KEY) or (
                    request.operator is not None
                )
                request.session[SESSION_HASH_KEY] = _session_hash(operator)
                request.session[SESSION_MFA_KEY] = True
                request.session.pop(SESSION_AUTH_AT_KEY, None)
                if not was_signed_in:
                    next_url = request.session.pop("orvahr_console_next", "")
                    request.session.cycle_key()
                    operator.last_login = timezone.now()
                    operator.save(update_fields=["last_login"])
                    services.record_action(
                        "login",
                        operator=operator,
                        ip_address=_ip(request),
                        method="enrolment",
                    )
                    request.session["orvahr_console_next"] = next_url
                request.operator = operator
        context = {
            "enrolled": False,
            "secret": secret,
            "secret_grouped": totp.grouped(secret),
            "otpauth_uri": totp.provisioning_uri(secret, operator.username),
        }
    else:
        context = {"enrolled": True}
        if request.method == "POST" and action in ("regenerate", "disable"):
            if twofactor.is_locked(operator):
                error = _("Too many wrong codes. Try again in 15 minutes.")
            elif not form.is_valid() or not twofactor.check_totp(
                operator, form.cleaned_data["code"]
            ):
                twofactor.register_failure(
                    operator, ip_address=_ip(request), reason=f"totp:{action}"
                )
                error = _("Invalid code.")
            elif action == "regenerate":
                twofactor.clear_failures(operator)
                codes = twofactor.new_recovery_codes(operator, ip_address=_ip(request))
            elif conf.two_factor_required():
                error = _("Two-factor authentication is mandatory for operators.")
            else:
                twofactor.disable(
                    operator, operator_acting=operator, ip_address=_ip(request)
                )
                request.session[SESSION_HASH_KEY] = _session_hash(operator)
                request.session[SESSION_MFA_KEY] = False
                messages.success(request, _("Two-factor authentication is off."))
                return HttpResponseRedirect(reverse("saas-console-2fa"))
    context.update(
        {
            "form": form,
            "error": error,
            "codes": codes,
            "required": conf.two_factor_required(),
            "remaining": (
                twofactor.remaining_recovery_codes(operator) if operator.has_totp else 0
            ),
            "continue_url": _safe_next(
                request, request.session.get("orvahr_console_next", "")
            ),
        }
    )
    response = render_console(
        request,
        "orvahr_saas/console/two_factor.html",
        context,
        status=200 if error is None else 400,
    )
    response["Cache-Control"] = "no-store"
    return response


@require_POST
def logout_view(request):
    operator = session_operator(request)
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
            "extend_form": ExtendTrialForm(initial={"until": tenant.trial_ends_on}),
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


@require_POST
@operator_required
def tenant_activate(request, pk):
    tenant = _tenant_or_404(pk)
    try:
        services.activate_tenant(
            tenant, operator=request.operator, ip_address=_ip(request)
        )
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
    else:
        messages.success(request, _("Tenant activated."))
    return HttpResponseRedirect(reverse("saas-console-tenant", args=[pk]))


@require_POST
@operator_required
def tenant_extend_trial(request, pk):
    tenant = _tenant_or_404(pk)
    form = ExtendTrialForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Please enter a valid date."))
    else:
        try:
            services.extend_trial(
                tenant,
                form.cleaned_data["until"],
                operator=request.operator,
                ip_address=_ip(request),
            )
        except ValidationError as exc:
            messages.error(request, exc.messages[0])
        else:
            messages.success(request, _("Trial extended."))
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


# ------------------------------------------------- plan change requests
# Tenants ask for another plan on their subscription page (orvahr_account);
# the requests live in each tenant's schema. Rows are addressed only by
# (tenant pk, token).


@operator_required
def plan_request_list(request):
    from orvahr_account.services import pending_plan_change_requests
    from orvahr_saas.models import Plan

    pending = pending_plan_change_requests()
    plans = {p.code: p for p in Plan.objects.all()}
    rows = [
        {
            "req": r,
            "current_plan": plans.get(r.current_plan_code),
            "requested_plan": plans.get(r.requested_plan_code),
        }
        for r in pending
    ]
    return render_console(
        request,
        "orvahr_saas/console/plan_requests.html",
        {"rows": rows, "billing_installed": _billing_installed()},
    )


def _billing_installed():
    from django.apps import apps

    return apps.is_installed("orvahr_billing")


def _decide_plan_request(request, tenant_pk, token, *, approve):
    from orvahr_account.services import NoticeError, decide_plan_change_request

    tenant = _tenant_or_404(tenant_pk)
    form = PlanRequestDecisionForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("The note is too long."))
        return HttpResponseRedirect(reverse("saas-console-plan-requests"))
    apply = approve and form.cleaned_data["apply"]
    note = form.cleaned_data["note"]
    try:
        decided = decide_plan_change_request(
            tenant,
            str(token),
            approve=approve,
            by=request.operator.username,
            note=note,
            apply=apply,
        )
    except NoticeError as exc:
        messages.error(request, str(exc))
        return HttpResponseRedirect(reverse("saas-console-plan-requests"))
    services.record_action(
        "approve_plan_change" if approve else "reject_plan_change",
        operator=request.operator,
        tenant=tenant,
        ip_address=_ip(request),
        token=str(token),
        current=decided.current_plan_code,
        requested=decided.requested_plan_code,
        applied=apply if approve else None,
        note=note or None,
    )
    if not approve:
        messages.success(request, _("Plan change request rejected."))
    elif apply:
        messages.success(
            request,
            _(
                "Plan change approved and applied. Update the price of the "
                "billing subscription of %(tenant)s."
            )
            % {"tenant": tenant.name},
        )
    else:
        messages.success(
            request,
            _(
                "Plan change approved (not applied yet). Change the plan on the "
                "tenant page and update the billing subscription of %(tenant)s."
            )
            % {"tenant": tenant.name},
        )
    return HttpResponseRedirect(reverse("saas-console-plan-requests"))


@require_POST
@operator_required
def plan_request_approve(request, tenant_pk, token):
    return _decide_plan_request(request, tenant_pk, token, approve=True)


@require_POST
@operator_required
def plan_request_reject(request, tenant_pk, token):
    return _decide_plan_request(request, tenant_pk, token, approve=False)


# ------------------------------------------------------------ notifications


@operator_required
def notification_log(request):
    from orvahr_saas.models import NotificationLog

    qs = NotificationLog.objects.select_related("tenant")
    event = request.GET.get("event")
    if event:
        qs = qs.filter(event=event)
    page = Paginator(qs, 100).get_page(request.GET.get("page"))
    return render_console(
        request, "orvahr_saas/console/notifications.html", {"page": page}
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
