"""
Control-plane operations (public schema). The console calls these, and so
may other apps -- orvahr_billing suspends overdue tenants with
``suspend_tenant(tenant, reason, actor_label="billing")``.

Every change is written to the OperatorAction audit trail.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
from datetime import timedelta
from io import StringIO

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from horilla.tenancy import TenantAwareThread, public_schema_name, use_schema

logger = logging.getLogger(__name__)

USAGE_CACHE_SECONDS = 300


def _models():
    from orvahr_saas import models

    return models


# -------------------------------------------------------------------- audit


def record_action(
    action,
    *,
    operator=None,
    actor_label="",
    tenant=None,
    tenant_request=None,
    ip_address=None,
    **detail,
):
    m = _models()
    with use_schema(public_schema_name()):
        return m.OperatorAction.objects.create(
            operator=operator,
            actor_label=actor_label if operator is None else "",
            action=action,
            tenant=tenant,
            tenant_request=tenant_request,
            ip_address=ip_address,
            detail={k: v for k, v in detail.items() if v is not None},
        )


# ---------------------------------------------------------- suspend/resume


def suspend_tenant(
    tenant, reason, *, operator=None, actor_label="system", ip_address=None
):
    """
    Block every request to ``tenant``'s hosts (TenantMiddleware answers 403;
    background jobs skip it). Data is untouched. Idempotent: suspending a
    suspended tenant only updates the reason.
    """
    m = _models()
    reason = (reason or "").strip()[:255]
    with use_schema(public_schema_name()), transaction.atomic():
        tenant = m.Tenant.objects.select_for_update().get(pk=tenant.pk)
        newly_suspended = tenant.status != m.Tenant.STATUS_SUSPENDED
        if newly_suspended:
            tenant.status_before_suspension = tenant.status
            tenant.suspended_at = timezone.now()
        tenant.status = m.Tenant.STATUS_SUSPENDED
        tenant.suspension_reason = reason
        tenant.save(
            update_fields=[
                "status",
                "status_before_suspension",
                "suspended_at",
                "suspension_reason",
            ]
        )
        record_action(
            "suspend",
            operator=operator,
            actor_label=actor_label,
            tenant=tenant,
            ip_address=ip_address,
            reason=reason,
        )
        if newly_suspended:
            from orvahr_saas import notifications

            notifications.tenant_suspended(tenant)  # sent after commit
    return tenant


def resume_tenant(tenant, *, operator=None, actor_label="system", ip_address=None):
    """Undo ``suspend_tenant``: back to the status it had (trial or active)."""
    m = _models()
    with use_schema(public_schema_name()), transaction.atomic():
        tenant = m.Tenant.objects.select_for_update().get(pk=tenant.pk)
        if tenant.status != m.Tenant.STATUS_SUSPENDED:
            return tenant
        previous = tenant.status_before_suspension
        # A trial that ended before the suspension stays ended (locked to the
        # account pages) until it is activated or extended.
        if previous not in (
            *m.Tenant.SCHEDULABLE_STATUSES,
            m.Tenant.STATUS_TRIAL_EXPIRED,
        ):
            previous = m.Tenant.STATUS_ACTIVE
        tenant.status = previous
        tenant.status_before_suspension = ""
        tenant.suspended_at = None
        tenant.suspension_reason = ""
        tenant.save(
            update_fields=[
                "status",
                "status_before_suspension",
                "suspended_at",
                "suspension_reason",
            ]
        )
        record_action(
            "resume",
            operator=operator,
            actor_label=actor_label,
            tenant=tenant,
            ip_address=ip_address,
            status=previous,
        )
        from orvahr_saas import notifications

        notifications.tenant_resumed(tenant)  # sent after commit
    return tenant


# ------------------------------------------------------------ trial lifecycle


def expire_trial(tenant, *, operator=None, actor_label="system", ip_address=None):
    """
    Trial over: status ``trial_expired``. Users can still sign in and reach
    the account pages (TenantMiddleware); background jobs skip the tenant.
    Nothing is deleted. Returns True when the status changed.
    """
    m = _models()
    with use_schema(public_schema_name()), transaction.atomic():
        tenant = m.Tenant.objects.select_for_update().get(pk=tenant.pk)
        if tenant.status != m.Tenant.STATUS_TRIAL:
            return False
        tenant.status = m.Tenant.STATUS_TRIAL_EXPIRED
        tenant.trial_expired_at = timezone.now()
        tenant.save(update_fields=["status", "trial_expired_at"])
        record_action(
            "trial_expired",
            operator=operator,
            actor_label=actor_label,
            tenant=tenant,
            ip_address=ip_address,
            trial_ends_on=str(tenant.trial_ends_on or ""),
        )
    return True


def activate_tenant(tenant, *, operator=None, actor_label="system", ip_address=None):
    """Trial (running or ended) -> active (a paying customer)."""
    m = _models()
    with use_schema(public_schema_name()), transaction.atomic():
        tenant = m.Tenant.objects.select_for_update().get(pk=tenant.pk)
        if tenant.status not in (m.Tenant.STATUS_TRIAL, m.Tenant.STATUS_TRIAL_EXPIRED):
            raise ValidationError(_("Only trial tenants can be activated."))
        old = tenant.status
        tenant.status = m.Tenant.STATUS_ACTIVE
        tenant.save(update_fields=["status"])
        record_action(
            "activate",
            operator=operator,
            actor_label=actor_label,
            tenant=tenant,
            ip_address=ip_address,
            old_status=old,
        )
    return tenant


def extend_trial(
    tenant, until, *, operator=None, actor_label="system", ip_address=None
):
    """Move the trial end to ``until``; an ended trial becomes a trial again."""
    m = _models()
    if until < timezone.localdate():
        raise ValidationError(_("The new trial end cannot be in the past."))
    with use_schema(public_schema_name()), transaction.atomic():
        tenant = m.Tenant.objects.select_for_update().get(pk=tenant.pk)
        if tenant.status not in (m.Tenant.STATUS_TRIAL, m.Tenant.STATUS_TRIAL_EXPIRED):
            raise ValidationError(
                _("Only trial tenants can have their trial extended.")
            )
        old = {
            "status": tenant.status,
            "trial_ends_on": str(tenant.trial_ends_on or ""),
        }
        tenant.status = m.Tenant.STATUS_TRIAL
        tenant.trial_ends_on = until
        tenant.trial_expired_at = None
        tenant.save(update_fields=["status", "trial_ends_on", "trial_expired_at"])
        record_action(
            "extend_trial",
            operator=operator,
            actor_label=actor_label,
            tenant=tenant,
            ip_address=ip_address,
            old=old,
            trial_ends_on=str(until),
        )
    return tenant


def change_plan(
    tenant,
    plan,
    *,
    max_employees=None,
    operator=None,
    actor_label="system",
    ip_address=None,
):
    """Move ``tenant`` to ``plan`` (and set/clear its employee override)."""
    m = _models()
    with use_schema(public_schema_name()), transaction.atomic():
        tenant = m.Tenant.objects.select_for_update().get(pk=tenant.pk)
        old = {"plan": tenant.plan_code, "max_employees": tenant.max_employees}
        tenant.plan = plan
        tenant.max_employees = max_employees
        tenant.save(update_fields=["plan", "plan_code", "max_employees"])
        record_action(
            "change_plan",
            operator=operator,
            actor_label=actor_label,
            tenant=tenant,
            ip_address=ip_address,
            old=old,
            new={"plan": tenant.plan_code, "max_employees": max_employees},
        )
    return tenant


# -------------------------------------------------------------------- usage


def _usage_key(tenant):
    return f"orvahr_saas:usage:{tenant.schema_name}"


def tenant_usage(tenant, refresh=False):
    """
    ``{"active_employees": n}`` for the console -- counts only, never
    personal data. Counted inside the tenant's schema, cached for 5 minutes
    (the cache key is made on the public schema).
    """
    key = _usage_key(tenant)
    with use_schema(public_schema_name()):
        if not refresh:
            cached = cache.get(key)
            if cached is not None:
                return cached
    usage = {"active_employees": None}
    try:
        with use_schema(tenant.schema_name):
            from employee.models import Employee

            usage["active_employees"] = (
                Employee.objects.entire().filter(is_active=True).count()
            )
    except Exception:  # noqa: BLE001 - one broken schema must not hide the list
        logger.exception("Could not count employees of %s", tenant.schema_name)
    with use_schema(public_schema_name()):
        cache.set(key, usage, USAGE_CACHE_SECONDS)
    return usage


# --------------------------------------------------------------- sign-up


def slug_availability(slug):
    """
    ``(available, message)`` for a requested sub-domain. Refuses invalid and
    reserved names, existing tenants/schemas/domains, and names already held
    by an open sign-up request.
    """
    from django_tenants.utils import schema_exists

    m = _models()
    slug = (slug or "").strip().lower()
    try:
        m.validate_slug(slug)
    except ValidationError as exc:
        return False, exc.messages[0]
    if slug in m.RESERVED_SLUGS:
        return False, _("This name is reserved. Please choose another.")
    schema = m.schema_name_for_slug(slug)
    with use_schema(public_schema_name()):
        taken = (
            m.Tenant.objects.filter(slug=slug).exists()
            or m.Tenant.objects.filter(schema_name=schema).exists()
            or m.TenantRequest.objects.filter(
                slug=slug, status__in=m.TenantRequest.HOLDS_SLUG
            ).exists()
            or m.Domain.objects.filter(domain=default_domain_for(slug)).exists()
            or schema_exists(schema)
        )
    if taken:
        return False, _("This name is already taken.")
    return True, _("Available")


def default_domain_for(slug):
    base = getattr(settings, "ORVAHR_SAAS_BASE_DOMAIN", "") or "localhost"
    return f"{slug}.{base}"


# ------------------------------------------------------------ provisioning


def start_provisioning(
    tenant_request,
    *,
    admin_password,
    domain,
    plan=None,
    status="trial",
    thai_setup=True,
    operator=None,
    ip_address=None,
    run_async=True,
):
    """
    Create the tenant for an approved request by running the existing
    ``create_tenant`` command (schema + migrations + company + admin) on a
    background thread; progress lands in ``tenant_request.provision_log`` /
    ``status``. The admin password is handed over through a one-off
    environment variable (create_tenant never takes it as an argument) that
    is removed as soon as the command has read it -- it is never stored.
    """
    m = _models()
    if tenant_request.status != m.TenantRequest.STATUS_APPROVED:
        raise ValidationError(_("Only approved requests can be provisioned."))
    updated = m.TenantRequest.objects.filter(
        pk=tenant_request.pk, status=m.TenantRequest.STATUS_APPROVED
    ).update(
        status=m.TenantRequest.STATUS_PROVISIONING,
        provision_log=_("Queued...") + "\n",
    )
    if not updated:  # someone else just started it
        raise ValidationError(_("This request is already being provisioned."))
    tenant_request.refresh_from_db()
    record_action(
        "provision_start",
        operator=operator,
        tenant_request=tenant_request,
        ip_address=ip_address,
        slug=tenant_request.slug,
        domain=domain,
        plan=plan.code if plan else None,
        status=status,
    )
    kwargs = {
        "request_pk": tenant_request.pk,
        "admin_password": admin_password,
        "domain": domain,
        "plan_code": plan.code if plan else "",
        "status": status,
        "thai_setup": thai_setup,
        "operator_pk": operator.pk if operator else None,
    }
    if not run_async:
        _provision(**kwargs)
        return None
    thread = TenantAwareThread(
        target=_provision, kwargs=kwargs, name=f"provision-{tenant_request.slug}"
    )
    thread.daemon = True
    thread.start()
    return thread


class _LogStream(StringIO):
    """Collects command output and mirrors it into the request row."""

    def __init__(self, request_pk):
        super().__init__()
        self.request_pk = request_pk

    def write(self, text):
        n = super().write(text)
        if "\n" in text:
            self.flush_to_db()
        return n

    def flush_to_db(self):
        m = _models()
        m.TenantRequest.objects.filter(pk=self.request_pk).update(
            provision_log=self.getvalue()[-20000:]
        )


def _provision(
    request_pk, admin_password, domain, plan_code, status, thai_setup, operator_pk
):
    from django.core.management import call_command

    m = _models()
    env_name = "ORVAHR_PROVISION_PW_" + secrets.token_hex(8).upper()
    out = _LogStream(request_pk)
    try:
        connection.set_schema_to_public()
        req = m.TenantRequest.objects.get(pk=request_pk)
        os.environ[env_name] = admin_password
        options = {
            "slug": req.slug,
            "name": req.company_name,
            "domain": domain,
            "admin_email": req.contact_email,
            "admin_first_name": (req.contact_name.split() or ["Admin"])[0][:150],
            "admin_password_env": env_name,
            "status": status,
            "plan": plan_code,
            "contact_email": req.contact_email,
            "thai_setup": thai_setup,
            "stdout": out,
            "stderr": out,
        }
        try:
            call_command("create_tenant", **options)
        finally:
            os.environ.pop(env_name, None)
        connection.set_schema_to_public()
        tenant = m.Tenant.objects.get(slug=req.slug)
        plan = tenant.plan
        if status == m.Tenant.STATUS_TRIAL and plan and plan.trial_days:
            tenant.trial_ends_on = timezone.localdate() + timedelta(
                days=plan.trial_days
            )
            m.Tenant.objects.filter(pk=tenant.pk).update(
                trial_ends_on=tenant.trial_ends_on
            )
        out.flush_to_db()
        m.TenantRequest.objects.filter(pk=request_pk).update(
            status=m.TenantRequest.STATUS_PROVISIONED, tenant=tenant
        )
        record_action(
            "provision_done",
            operator=m.ConsoleOperator.objects.filter(pk=operator_pk).first(),
            tenant=tenant,
            tenant_request=req,
        )
        from orvahr_saas import notifications

        req.refresh_from_db()
        notifications.tenant_provisioned(req, tenant)
    except BaseException as exc:  # noqa: BLE001 - recorded, thread ends here
        logger.exception("Provisioning request %s failed", request_pk)
        connection.set_schema_to_public()
        out.write(f"\nERROR: {exc}\n")
        out.flush_to_db()
        m.TenantRequest.objects.filter(pk=request_pk).update(
            status=m.TenantRequest.STATUS_FAILED
        )
        record_action(
            "provision_failed",
            operator=m.ConsoleOperator.objects.filter(pk=operator_pk).first(),
            tenant_request=m.TenantRequest.objects.filter(pk=request_pk).first(),
            error=str(exc)[:500],
        )
    finally:
        if threading.current_thread() is not threading.main_thread() and getattr(
            threading.current_thread(), "name", ""
        ).startswith("provision-"):
            connection.close()  # this thread's own connection
