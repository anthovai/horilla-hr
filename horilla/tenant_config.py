"""
Per-tenant runtime configuration that used to be process-global (SaaS mode).

Several features read their configuration from the database once, at
startup or when an admin saves it, and keep it in process memory: mail
automations and PMS bonus-point automations (dynamic signal handlers), the
audit-log model list. With one schema per tenant that is wrong twice over:
at startup the connection is on the public schema (nothing to read), and
whatever is read afterwards belongs to whichever tenant saved last.

Two helpers fix that without touching the single-tenant code path (callers
only use them when ``horilla.tenancy.is_multi_tenant()``):

``TenantConfigCache(name, loader)``
    ``get()`` returns ``loader()`` evaluated inside the *active* schema,
    cached per schema in this process. ``invalidate()`` (call it when the
    tenant edits the configuration) drops this schema's entry and bumps a
    version in the Django cache -- which is schema-prefixed in SaaS mode --
    so other worker processes reload within ``recheck_seconds`` when the
    cache is shared (Redis); with a per-process cache they reload on their
    own next invalidation or restart, exactly as before.

``TenantSignalRegistry(name, loader, signals)``
    For code that builds signal handlers from configuration rows. Instead of
    connecting each handler to the signal (process-wide, for every tenant),
    the loader hands them to ``registry.connect(signal, handler, sender)``;
    one static receiver per signal looks up the handlers of the active
    schema and calls them. Each tenant's handlers therefore fire only in its
    own schema, every tenant's handlers are live at the same time, and a
    schema's handlers are loaded lazily the first time that schema sends one
    of the signals -- no queries at startup.

A loader that fails (e.g. its table does not exist yet while the schema is
being migrated) returns the default and is retried after ``retry_seconds``
instead of caching the failure. Loading runs in a savepoint so a failed query
never poisons the caller's transaction.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid

from django.core.cache import cache
from django.db import DatabaseError, transaction

from horilla.tenancy import current_schema, public_schema_name

logger = logging.getLogger(__name__)

_MISSING = object()


def _tenant_schema():
    schema = current_schema()
    if not schema or schema == public_schema_name():
        return None
    return schema


class TenantConfigCache:
    def __init__(
        self, name, loader, default=None, recheck_seconds=30, retry_seconds=5
    ):
        self.name = name
        self.loader = loader
        self.default = default
        self.recheck_seconds = recheck_seconds
        self.retry_seconds = retry_seconds
        # schema -> (value, version, checked_at)
        self._entries = {}
        # schema -> monotonic time of the last failed load
        self._failed = {}
        self._lock = threading.RLock()
        self._local = threading.local()

    # ------------------------------------------------------------ internals
    @property
    def _version_key(self):
        # The cache KEY_FUNCTION prefixes the active schema in SaaS mode.
        return f"orvahr:tenant-config:{self.name}:version"

    def _read_version(self):
        try:
            return cache.get(self._version_key)
        except Exception:  # noqa: BLE001 - a cache outage must not break saves
            logger.warning("tenant config %s: cache unavailable", self.name)
            return None

    def _load(self, schema):
        if getattr(self._local, "loading", False):
            # A save made by the loader itself: don't recurse.
            return _MISSING
        self._local.loading = True
        try:
            with transaction.atomic():
                return self.loader()
        except DatabaseError as exc:
            logger.debug("tenant config %s not loaded for %s: %s", self.name, schema, exc)
            return _MISSING
        finally:
            self._local.loading = False

    # --------------------------------------------------------------- public
    def loading(self) -> bool:
        """True while this thread is running the loader."""
        return getattr(self._local, "loading", False)

    def get(self):
        """The active schema's configuration (``default`` on public/no tenant)."""
        schema = _tenant_schema()
        if schema is None:
            return self.default
        now = time.monotonic()
        entry = self._entries.get(schema)
        if entry is not None:
            value, version, checked_at = entry
            if now - checked_at < self.recheck_seconds:
                return value
            current = self._read_version()
            if current == version:
                self._entries[schema] = (value, version, now)
                return value
        failed_at = self._failed.get(schema)
        if failed_at is not None and now - failed_at < self.retry_seconds:
            return self.default
        with self._lock:
            version = self._read_version()
            value = self._load(schema)
            if value is _MISSING:
                self._failed[schema] = now
                return self.default
            self._failed.pop(schema, None)
            self._entries[schema] = (value, version, now)
            return value

    def invalidate(self):
        """The active tenant changed its configuration."""
        schema = _tenant_schema()
        if schema is None:
            return
        self._entries.pop(schema, None)
        self._failed.pop(schema, None)

        def bump():
            try:
                cache.set(self._version_key, uuid.uuid4().hex, None)
            except Exception:  # noqa: BLE001
                logger.warning("tenant config %s: cache unavailable", self.name)

        # After commit, so another process can't reload the old rows and
        # store them under the new version.
        transaction.on_commit(bump)

    def clear(self):
        """Forget every schema (tests)."""
        self._entries.clear()
        self._failed.clear()

    def loaded_schemas(self):
        return sorted(self._entries)


class TenantSignalRegistry:
    """See the module docstring."""

    def __init__(self, name, loader, signals, recheck_seconds=30):
        self.name = name
        self._user_loader = loader
        self._collector = threading.local()
        self.config = TenantConfigCache(
            name, self._collect, default={}, recheck_seconds=recheck_seconds
        )
        for signal in signals:
            signal.connect(
                self._dispatch,
                weak=False,
                dispatch_uid=f"orvahr-tenant-signals-{name}-{id(signal)}",
            )

    def _collect(self):
        handlers = {}
        self._collector.handlers = handlers
        try:
            self._user_loader()
        finally:
            self._collector.handlers = None
        return handlers

    def connect(self, signal, receiver, sender):
        """Called by the loader instead of ``signal.connect(receiver, sender=...)``."""
        handlers = getattr(self._collector, "handlers", None)
        if handlers is None:
            raise RuntimeError(
                f"{self.name}: connect() is only valid inside the registry loader"
            )
        handlers.setdefault((signal, sender), []).append(receiver)

    def invalidate(self):
        self.config.invalidate()

    def handlers_for(self, signal, sender):
        return self.config.get().get((signal, sender), ())

    def _dispatch(self, signal, sender, **kwargs):
        if self.config.loading() or _tenant_schema() is None:
            return
        for receiver in self.handlers_for(signal, sender):
            receiver(signal=signal, sender=sender, **kwargs)
