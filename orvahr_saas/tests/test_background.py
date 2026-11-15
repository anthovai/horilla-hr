"""Scheduler wrapper, thread helper, cache keys, media paths, startup guards."""

import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

from django.core.cache import cache
from django.db import connection
from django.test import override_settings
from django_tenants.utils import schema_context

from horilla import tenancy
from horilla.scheduling import ScheduledJob, scheduler_target
from orvahr_saas.tests.base import SaasTestCase, suspend

SEEN = []


def record_schema():
    """A scheduled job: records the schema it ran in."""
    SEEN.append(connection.schema_name)


def fail_in_alpha():
    SEEN.append(connection.schema_name)
    if connection.schema_name == "t_alpha":
        raise RuntimeError("boom")


def count_companies():
    from base.models import Company

    SEEN.append((connection.schema_name, Company.objects.count()))


class SchedulerTests(SaasTestCase):
    def setUp(self):
        super().setUp()
        SEEN.clear()

    def test_target_wraps_per_tenant_and_public_jobs(self):
        func, args = scheduler_target(
            ScheduledJob(func=record_schema, trigger="interval", job_id="x")
        )
        self.assertIs(func, tenancy.run_job_for_each_tenant)
        self.assertEqual(args, [f"{__name__}:record_schema"])
        func, args = scheduler_target(
            ScheduledJob(
                func=record_schema, trigger="interval", job_id="y", per_tenant=False
            )
        )
        self.assertIs(func, tenancy.run_job_in_public_schema)

    def test_job_runs_once_per_active_or_trial_tenant(self):
        tenancy.run_job_for_each_tenant(f"{__name__}:record_schema")
        self.assertIn("t_alpha", SEEN)
        self.assertIn("t_beta", SEEN)  # trial runs too
        self.assertNotIn("public", SEEN)
        self.assertEqual(len(SEEN), len(set(SEEN)))

    def test_suspended_tenant_is_skipped(self):
        suspend(self.beta)
        tenancy.run_job_for_each_tenant(f"{__name__}:record_schema")
        self.assertIn("t_alpha", SEEN)
        self.assertNotIn("t_beta", SEEN)

    def test_job_queries_hit_each_tenants_own_tables(self):
        from base.models import Company

        with schema_context("t_alpha"):
            Company.objects.create(company="Only In Alpha")
        tenancy.run_job_for_each_tenant(f"{__name__}:count_companies")
        counts = dict(SEEN)
        self.assertEqual(counts["t_alpha"], counts["t_beta"] + 1)

    def test_one_tenant_failing_does_not_stop_the_others(self):
        with self.assertRaises(tenancy.TenantJobError):
            tenancy.run_job_for_each_tenant(f"{__name__}:fail_in_alpha")
        self.assertIn("t_beta", SEEN)

    def test_connection_restored_after_run(self):
        connection.set_tenant(self.beta)
        tenancy.run_job_for_each_tenant(f"{__name__}:record_schema")
        self.assertEqual(connection.schema_name, "t_beta")

    def test_public_job_runs_once_on_public(self):
        tenancy.run_job_in_public_schema(f"{__name__}:record_schema")
        self.assertEqual(SEEN, ["public"])


def _thread_probe(result):
    from base.models import Company

    try:
        result["schema"] = connection.schema_name
        result["count"] = Company.objects.count()
    finally:
        connection.close()


class _ProbeThread(tenancy.TenantAwareThread):
    def __init__(self, result):
        tenancy.TenantAwareThread.__init__(self)
        self.result = result

    def run(self):
        _thread_probe(self.result)


class _LegacyInitThread(tenancy.TenantAwareThread):
    """Mirrors leave/helpdesk threads that call Thread.__init__ directly."""

    def __init__(self, result):
        import threading

        threading.Thread.__init__(self)
        self.result = result

    def run(self):
        _thread_probe(self.result)


class ThreadTests(SaasTestCase):
    def test_target_thread_runs_in_creating_tenant(self):
        for tenant in (self.alpha, self.beta):
            connection.set_tenant(tenant)
            result = {}
            thread = tenancy.TenantAwareThread(target=_thread_probe, args=(result,))
            thread.start()
            thread.join()
            self.assertEqual(result["schema"], tenant.schema_name)
            self.assertIn("count", result)  # tenant table reachable

    def test_subclass_overriding_run_is_wrapped(self):
        connection.set_tenant(self.beta)
        for cls in (_ProbeThread, _LegacyInitThread):
            result = {}
            thread = cls(result)
            thread.start()
            thread.join()
            self.assertEqual(result["schema"], "t_beta", cls.__name__)

    def test_plain_thread_would_be_on_public(self):
        """The bug the helper fixes: a bare Thread loses the schema."""
        import threading

        connection.set_tenant(self.alpha)
        result = {}

        def probe():
            result["schema"] = connection.schema_name
            connection.close()

        thread = threading.Thread(target=probe)
        thread.start()
        thread.join()
        self.assertEqual(result["schema"], "public")

    def test_tenant_aware_with_executor(self):
        connection.set_tenant(self.alpha)

        def probe():
            try:
                return connection.schema_name
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            schemas = list(pool.map(lambda f: f(), [tenancy.tenant_aware(probe)] * 3))
        self.assertEqual(set(schemas), {"t_alpha"})

    def test_schema_key(self):
        connection.set_tenant(self.alpha)
        self.assertEqual(tenancy.schema_key(7), ("t_alpha", 7))
        connection.set_tenant(self.beta)
        self.assertEqual(tenancy.schema_key(7), ("t_beta", 7))


class CacheTests(SaasTestCase):
    def test_keys_differ_per_tenant(self):
        with schema_context("t_alpha"):
            key_a = cache.make_key("perm:42")
            cache.set("perm:42", "alpha-value")
        with schema_context("t_beta"):
            key_b = cache.make_key("perm:42")
            self.assertIsNone(cache.get("perm:42"))
            cache.set("perm:42", "beta-value")
        with schema_context("t_alpha"):
            self.assertEqual(cache.get("perm:42"), "alpha-value")
        self.assertNotEqual(key_a, key_b)
        self.assertTrue(key_a.startswith("t_alpha:"))


class MediaTests(SaasTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()

    def test_storage_writes_into_tenant_folder_with_unprefixed_url(self):
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage

        self.assertIsInstance(default_storage, tenancy.TenantMediaStorage)
        with override_settings(MEDIA_ROOT=self.tmp):
            with schema_context("t_alpha"):
                name = default_storage.save("docs/a.txt", ContentFile(b"alpha"))
                path_a = default_storage.path(name)
                url = default_storage.url(name)
            with schema_context("t_beta"):
                self.assertFalse(default_storage.exists(name))
                self.assertEqual(
                    tenancy.tenant_media_root(), os.path.join(self.tmp, "t_beta", "")
                )
        self.assertEqual(path_a, os.path.join(self.tmp, "t_alpha", "docs", "a.txt"))
        self.assertTrue(os.path.exists(path_a))
        self.assertEqual(url, "/media/docs/a.txt")

    def test_protected_media_resolves_inside_requesting_tenant(self):
        from django.test import RequestFactory

        from base.views import protected_media

        for schema, body in (("t_alpha", b"alpha"), ("t_beta", b"beta")):
            folder = os.path.join(self.tmp, schema, "base", "icon")
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, "logo.png"), "wb") as fh:
                fh.write(body)
        with override_settings(MEDIA_ROOT=self.tmp):
            for schema, body in (("t_alpha", b"alpha"), ("t_beta", b"beta")):
                with schema_context(schema):
                    request = RequestFactory().get("/media/base/icon/logo.png")
                    response = protected_media(request, "base/icon/logo.png")
                    self.assertEqual(b"".join(response.streaming_content), body)

    def test_public_schema_media_root_is_base(self):
        with override_settings(MEDIA_ROOT=self.tmp), schema_context("public"):
            self.assertEqual(tenancy.tenant_media_root(), self.tmp)


class StartupGuardTests(SaasTestCase):
    def test_app_has_tables(self):
        with schema_context("public"):
            self.assertFalse(tenancy.app_has_tables("employee"))
            self.assertFalse(tenancy.app_has_tables("horilla_ldap"))
            self.assertTrue(tenancy.app_has_tables("orvahr_saas"))
            self.assertTrue(tenancy.app_has_tables("contenttypes"))
        with schema_context("t_alpha"):
            self.assertTrue(tenancy.app_has_tables("employee"))

    def test_ready_hooks_do_not_query_on_public(self):
        """The startup code that used to query tenant tables is a no-op on public."""
        from django.test.utils import CaptureQueriesContext

        from horilla.config import load_ldap_settings
        from horilla_audit.registry import _load_targets
        from horilla_automations.signals import start_automation
        from pms.signals import start_automation as pms_start_automation

        with schema_context("public"), CaptureQueriesContext(connection) as ctx:
            load_ldap_settings()
            _load_targets()
            start_automation()
            pms_start_automation()
        selects = [q["sql"] for q in ctx.captured_queries if "SELECT" in q["sql"]]
        self.assertEqual(selects, [])

    def test_automation_handler_ignores_other_schema(self):
        connection.set_tenant(self.alpha)
        schema = tenancy.current_schema()
        self.assertTrue(tenancy.schema_matches(schema))
        connection.set_tenant(self.beta)
        self.assertFalse(tenancy.schema_matches(schema))
        self.assertTrue(tenancy.schema_matches(None))
