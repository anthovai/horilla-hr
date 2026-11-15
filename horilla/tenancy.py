"""
Tenant helpers shared by the whole code base.

OrvaHR can run as one PostgreSQL schema per customer (django-tenants) when
``ORVAHR_MULTI_TENANT`` is on. Everything in this module is written so that
with the flag OFF -- the default, and how every existing install runs -- each
helper is a no-op that returns exactly what the code did before:

* ``current_schema()`` is ``None``;
* ``TenantAwareThread`` is a plain ``threading.Thread``;
* ``tenant_aware(fn)`` returns a wrapper that just calls ``fn``;
* ``tenant_media_root()`` is ``settings.MEDIA_ROOT``;
* ``run_job_for_each_tenant`` calls the job once.

Nothing here imports django_tenants at module level, so the single-tenant
install does not need the package importable at all.

Why threads need this
---------------------
django-tenants keeps the active schema on the *connection*, and Django's
connections are thread-local. A thread started from a request gets a fresh
connection whose search_path is the public schema, so a mail thread that reads
``LeaveRequest`` would query ``public.leave_leaverequest`` -- which does not
exist in SaaS mode -- or, worse, another tenant's table if search_path were
ever wrong. The thread therefore captures the tenant when it is *created*
(on the request's thread) and re-activates it when it *runs*.
"""

from __future__ import annotations

import functools
import logging
import os
import threading
from contextlib import contextmanager, nullcontext

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.db import connection

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------- state


def is_multi_tenant() -> bool:
    """True when the process runs in SaaS (schema-per-tenant) mode."""
    return bool(getattr(settings, "ORVAHR_MULTI_TENANT", False))


def public_schema_name() -> str:
    return getattr(settings, "PUBLIC_SCHEMA_NAME", "public")


def current_schema():
    """The active schema name, or ``None`` in single-tenant mode."""
    if not is_multi_tenant():
        return None
    return getattr(connection, "schema_name", None)


def current_tenant():
    """The active tenant object (a Tenant row or FakeTenant), or ``None``."""
    if not is_multi_tenant():
        return None
    return getattr(connection, "tenant", None)


def in_public_schema() -> bool:
    """True only in SaaS mode while the connection points at the public schema."""
    schema = current_schema()
    return schema is not None and schema == public_schema_name()


def app_has_tables(app_label: str) -> bool:
    """
    Whether ``app_label``'s tables exist in the active schema.

    Always True in single-tenant mode. In SaaS mode the public schema holds
    only SHARED_APPS, so code that runs at startup or on post_migrate (which
    Django fires for *every* app on *every* schema migration) must skip tenant
    apps while the connection is on public instead of querying a missing table.
    """
    if not in_public_schema():
        return True
    from django.apps import apps

    try:
        config = apps.get_app_config(app_label)
    except LookupError:
        return False
    shared = getattr(settings, "SHARED_APPS", ())
    return config.name in shared or app_label in shared


def schema_key(*parts):
    """
    A key for process-global dicts (caches, "last run" maps, thread
    registries) that are indexed by a primary key. Primary keys repeat across
    tenants, so in SaaS mode the schema is prefixed; in single-tenant mode the
    key is unchanged (a single part stays a bare value).
    """
    schema = current_schema()
    if schema is None:
        return parts[0] if len(parts) == 1 else tuple(parts)
    return (schema, *parts)


# ------------------------------------------------------------ tenant context


def capture_tenant():
    """Snapshot the active tenant so another thread can re-activate it."""
    return current_tenant()


def activate(tenant):
    """
    Context manager that makes ``tenant`` (from ``capture_tenant``) active on
    this thread's connection, restoring the previous one afterwards. A no-op
    for ``None`` (single-tenant mode, or nothing captured).
    """
    if tenant is None or not is_multi_tenant():
        return nullcontext()
    from django_tenants.utils import tenant_context

    return tenant_context(tenant)


def use_schema(schema_name):
    """Context manager for a schema by name; no-op for ``None``/single-tenant."""
    if schema_name is None or not is_multi_tenant():
        return nullcontext()
    from django_tenants.utils import schema_context

    return schema_context(schema_name)


def tenant_aware(func):
    """
    Bind ``func`` to the tenant active *now*, for running later on another
    thread: ``ThreadPoolExecutor.submit(tenant_aware(fn), ...)``,
    ``transaction.on_commit(tenant_aware(...))``, ``scheduler.add_job(
    tenant_aware(fn), ...)``. In single-tenant mode it just calls ``func``.
    """
    tenant = capture_tenant()

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with activate(tenant):
            return func(*args, **kwargs)

    wrapper.orvahr_tenant = tenant
    return wrapper


class TenantAwareThread(threading.Thread):
    """
    ``threading.Thread`` that runs in the tenant that was active when it was
    constructed. Use it as a drop-in replacement, both as
    ``TenantAwareThread(target=...)`` and as a base class: subclasses that
    override ``run()`` are wrapped automatically (``__init_subclass__``), so
    the existing ``class MailSendThread(Thread): def run(self): ...`` classes
    only change their base.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.orvahr_tenant = capture_tenant()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        original = cls.__dict__.get("run")
        if original is None or getattr(original, "_orvahr_tenant_wrapped", False):
            return

        @functools.wraps(original)
        def run(self, *args, **kw):
            with activate(getattr(self, "orvahr_tenant", None)):
                return original(self, *args, **kw)

        run._orvahr_tenant_wrapped = True
        cls.run = run

    def start(self):
        # Several existing subclasses call ``Thread.__init__(self)`` directly,
        # bypassing our __init__. start() still runs on the creating thread,
        # so capturing here is equally correct.
        if getattr(self, "orvahr_tenant", None) is None:
            self.orvahr_tenant = capture_tenant()
        return super().start()

    def run(self):
        with activate(getattr(self, "orvahr_tenant", None)):
            return super().run()


# --------------------------------------------------------------- media files


def tenant_media_root() -> str:
    """
    MEDIA_ROOT for the active tenant: ``MEDIA_ROOT/<schema>/`` in SaaS mode,
    ``MEDIA_ROOT`` otherwise (and on the public schema, which stores nothing).

    Use this -- never ``settings.MEDIA_ROOT`` -- wherever code opens or lists
    uploaded files by path.
    """
    root = settings.MEDIA_ROOT
    schema = current_schema()
    if schema is None or schema == public_schema_name():
        return root
    return os.path.join(root, schema, "")


class TenantMediaStorage(FileSystemStorage):
    """
    FileSystemStorage whose location follows the active tenant.

    Files are stored under ``MEDIA_ROOT/<schema>/`` but the URL stays
    ``MEDIA_URL + name`` (no schema in it): ``/media/...`` is served by
    ``base.views.protected_media`` on the tenant's own host, which resolves the
    path against that same tenant's folder. So a stored name never leaks the
    schema, and a URL copied to another tenant's host resolves inside *that*
    tenant's folder, not the original one.
    """

    @property
    def base_location(self):
        if self._location is not None:
            return self._location
        return tenant_media_root()

    @property
    def location(self):
        return os.path.abspath(self.base_location)


# -------------------------------------------------------------- scheduling


class TenantJobError(Exception):
    """One or more tenants failed in a per-tenant scheduled job run."""


def iter_schedulable_tenants():
    """
    Tenants background jobs run for: every non-public tenant whose status is
    active or trial. Evaluated on the public schema.
    """
    from django_tenants.utils import get_tenant_model

    model = get_tenant_model()
    with use_schema(public_schema_name()):
        qs = model.objects.exclude(schema_name=public_schema_name())
        if hasattr(model, "SCHEDULABLE_STATUSES"):
            qs = qs.filter(status__in=model.SCHEDULABLE_STATUSES)
        return list(qs.order_by("schema_name"))


def run_job_for_each_tenant(func_ref: str, *args, **kwargs):
    """
    Entry point APScheduler stores for every per-tenant job.

    ``func_ref`` is the ``module:qualname`` of the real job (a textual
    reference, because DjangoJobStore pickles the job and a closure cannot be
    pickled). In single-tenant mode the job runs once, as before. In SaaS mode
    it runs once per active tenant inside that tenant's schema; one tenant
    failing does not stop the others, and the failures are raised together at
    the end so run_scheduler's error listener still reports them.
    """
    from apscheduler.util import ref_to_obj

    func = ref_to_obj(func_ref)
    if not is_multi_tenant():
        return func(*args, **kwargs)

    failures = []
    for tenant in iter_schedulable_tenants():
        with activate(tenant):
            try:
                func(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - reported below
                logger.exception(
                    "Scheduled job %s failed for tenant %s",
                    func_ref,
                    tenant.schema_name,
                )
                failures.append((tenant.schema_name, exc))
    if failures:
        names = ", ".join(name for name, _ in failures)
        raise TenantJobError(f"{func_ref} failed for tenant(s): {names}") from failures[
            0
        ][1]
    return None


def run_job_in_public_schema(func_ref: str, *args, **kwargs):
    """Entry point for ``per_tenant=False`` jobs (shared/public data only)."""
    from apscheduler.util import ref_to_obj

    func = ref_to_obj(func_ref)
    with use_schema(public_schema_name() if is_multi_tenant() else None):
        return func(*args, **kwargs)


def schema_matches(schema) -> bool:
    """
    For process-global signal handlers built from one tenant's rows (mail
    automations, bonus-point settings): True when the handler may act in the
    active schema. ``schema`` is ``current_schema()`` captured when the handler
    was built; ``None`` (single-tenant mode) always matches.
    """
    return schema is None or schema == current_schema()
