"""
Test runner that runs the ordinary suite *inside a tenant schema* (SaaS mode).

Selected automatically by ``horilla/settings/saas.py`` when
``ORVAHR_MULTI_TENANT`` is on; never used (or imported) in single-tenant mode.

The ~3,300 existing tests were written for one database. In SaaS mode every
app table except the shared ones lives in a tenant schema, so on the public
schema they fail with "relation does not exist". This runner:

1. creates the test database as usual (public schema migrated), then one test
   tenant -- schema ``testtenant``, host ``testtenant.localhost`` -- by cloning
   the pre-migrated template schema (``orvahr_saas.tenant_template``; built on
   first use, kept with ``--keepdb``) and running a real ``migrate_schemas``
   on it. With ``--keepdb`` the tenant is reused (``migrate`` brings it up to
   date like any other tenant);
2. before every test class (``setUpClass``, so ``setUpTestData`` is covered)
   and every test (``_pre_setup``) points the connection at that tenant;
3. makes every ``Client``/``RequestFactory`` built while such a test runs send
   ``HTTP_HOST: testtenant.localhost`` unless the test passes its own host,
   so ``TenantMiddleware`` routes the request to the same schema.

Tests that manage tenants themselves are left alone: django-tenants'
``TenantTestCase``/``FastTenantTestCase`` subclasses and everything in
``orvahr_saas``/``orvahr_billing`` (written for SaaS mode). Any class can opt
in or out explicitly with ``saas_test_tenant = True/False``.

Parallel runs (``--parallel N``) work: the tenant is created *before* the
test database is cloned, so every clone has it; workers (fork or spawn)
look the tenant up lazily.
"""

from __future__ import annotations

import functools
import unittest

from django.conf import settings
from django.db import connection
from django.test import SimpleTestCase
from django.test.client import RequestFactory
from django.test.runner import DiscoverRunner, ParallelTestSuite
from django.test.utils import setup_databases as _setup_databases

TEST_SCHEMA = "testtenant"
TEST_DOMAIN = "testtenant.localhost"
SELF_MANAGED_MODULES = ("orvahr_saas.", "orvahr_billing.")

# Per process: the Tenant row (looked up lazily so spawned workers work) and
# whether the test running now uses the harness (drives the Client host).
_state = {"tenant": None, "active": False}


# ------------------------------------------------------------------ tenant


def ensure_test_tenant(verbosity=0):
    """Create (or reuse) the test tenant and its domain. Returns the Tenant."""
    from django.core.management import call_command
    from django_tenants.utils import schema_exists

    from orvahr_saas import tenant_template
    from orvahr_saas.models import Domain, Tenant

    connection.set_schema_to_public()
    tenant = Tenant.objects.filter(schema_name=TEST_SCHEMA).first()
    if tenant is None:
        if not schema_exists(TEST_SCHEMA):
            template = tenant_template.template_schema_name()
            if not tenant_template.template_exists(template):
                if verbosity:
                    print(f"Building tenant template {template!r} (all migrations)...")
                tenant_template.build_template(template, verbosity=0)
            tenant_template.clone_template(TEST_SCHEMA, template)
            call_command(
                "migrate_schemas",
                schema_name=TEST_SCHEMA,
                interactive=False,
                verbosity=0,
            )
            connection.set_schema_to_public()
        tenant = Tenant(
            schema_name=TEST_SCHEMA,
            slug=TEST_SCHEMA,
            name="Test Tenant",
            status=Tenant.STATUS_ACTIVE,
        )
        tenant.auto_create_schema = False
        tenant.save()
    Domain.objects.get_or_create(
        domain=TEST_DOMAIN, defaults={"tenant": tenant, "is_primary": True}
    )
    _allow_host()
    return tenant


def _allow_host():
    hosts = settings.ALLOWED_HOSTS
    if "*" not in hosts and TEST_DOMAIN not in hosts:
        settings.ALLOWED_HOSTS = [*hosts, TEST_DOMAIN]


def test_tenant(refresh=False):
    """The test Tenant for this process (re-created if a flush removed it)."""
    tenant = _state["tenant"]
    if tenant is None or refresh:
        from orvahr_saas.models import Tenant

        connection.set_schema_to_public()
        tenant = Tenant.objects.filter(schema_name=TEST_SCHEMA).first()
        if tenant is None:
            # A TransactionTestCase on the public schema (orvahr_saas)
            # flushes the registry; the schema itself is untouched.
            tenant = ensure_test_tenant()
        elif refresh:
            from orvahr_saas.models import Domain

            Domain.objects.get_or_create(
                domain=TEST_DOMAIN, defaults={"tenant": tenant, "is_primary": True}
            )
        _state["tenant"] = tenant
    return tenant


def activate_test_tenant():
    connection.set_tenant(test_tenant())


# ------------------------------------------------------------ test classes


def uses_test_tenant(cls) -> bool:
    flag = getattr(cls, "saas_test_tenant", None)
    if flag is not None:
        return bool(flag)
    from django_tenants.test.cases import TenantTestCase

    if issubclass(cls, TenantTestCase):
        return False
    return not cls.__module__.startswith(SELF_MANAGED_MODULES)


def install():
    """Patch Django's test case and client classes (idempotent, per process)."""
    if getattr(SimpleTestCase, "_orvahr_saas_harness", False):
        return
    SimpleTestCase._orvahr_saas_harness = True
    _allow_host()

    original_setup_class = SimpleTestCase.setUpClass.__func__

    def setUpClass(cls):
        active = uses_test_tenant(cls)
        _state["active"] = active
        if active and cls.databases:
            # Re-checked once per class: a TransactionTestCase on the public
            # schema (orvahr_saas) may have flushed the tenant registry.
            test_tenant(refresh=True)
            activate_test_tenant()
        elif cls.databases:
            # Self-managed classes start where they would without the
            # harness: on the public schema, not on the previous class's
            # tenant (create_tenant refuses to run inside a tenant).
            connection.set_schema_to_public()
        return original_setup_class(cls)

    SimpleTestCase.setUpClass = classmethod(setUpClass)

    # SimpleTestCase._pre_setup (a classmethod) runs first in every test's
    # pre-setup chain -- before TransactionTestCase loads fixtures.
    original_pre_setup = SimpleTestCase._pre_setup.__func__

    def _pre_setup(cls):
        active = uses_test_tenant(cls)
        _state["active"] = active
        if active and cls.databases:
            activate_test_tenant()
        return original_pre_setup(cls)

    SimpleTestCase._pre_setup = classmethod(_pre_setup)

    original_rf_init = RequestFactory.__init__

    @functools.wraps(original_rf_init)
    def rf_init(self, *args, **kwargs):
        original_rf_init(self, *args, **kwargs)
        if _state["active"]:
            self.defaults.setdefault("HTTP_HOST", TEST_DOMAIN)

    RequestFactory.__init__ = rf_init


# ------------------------------------------------------------------ runner


class SkippedModuleTest(unittest.TestCase):
    """
    Stand-in for unittest's ``ModuleSkipped`` (a module that raised SkipTest
    on import, e.g. the flag-off proofs). unittest builds that class on the
    fly, so it cannot be pickled to a ``--parallel`` worker.
    """

    def __init__(self, module_name="", reason=""):
        super().__init__("runTest")
        self.module_name = module_name
        self.reason = reason

    def id(self):
        return self.module_name

    def __str__(self):
        return f"{self.module_name} (skipped module)"

    def runTest(self):
        raise unittest.SkipTest(self.reason)


def _picklable(suite):
    """Replace unittest's ModuleSkipped tests in ``suite`` (in place)."""
    tests = []
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            tests.append(_picklable(test))
        elif type(test).__name__ == "ModuleSkipped":
            name = test._testMethodName
            try:
                getattr(test, name)()
                reason = ""
            except unittest.SkipTest as exc:
                reason = str(exc)
            tests.append(SkippedModuleTest(name, reason))
        else:
            tests.append(test)
    suite._tests = tests
    return suite


class SaasParallelTestSuite(ParallelTestSuite):
    # Runs in spawned workers before django.setup(); fork inherits patches.
    def process_setup(*args):
        install()


class SaasTestRunner(DiscoverRunner):
    """DiscoverRunner that runs every ordinary test inside tenant ``testtenant``."""

    parallel_test_suite = SaasParallelTestSuite

    def load_tests_for_label(self, label, discover_kwargs):
        return _picklable(super().load_tests_for_label(label, discover_kwargs))

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        install()

    def setup_databases(self, **kwargs):
        # Create the test DB without cloning, add the tenant, then clone, so
        # every parallel worker's database has the tenant too.
        old_config = _setup_databases(
            self.verbosity,
            self.interactive,
            time_keeper=self.time_keeper,
            keepdb=self.keepdb,
            debug_sql=self.debug_sql,
            parallel=0,
            **kwargs,
        )
        with self.time_keeper.timed("  Creating test tenant"):
            ensure_test_tenant(self.verbosity)
            connection.set_schema_to_public()
        if self.parallel > 1:
            for index in range(self.parallel):
                with self.time_keeper.timed("  Cloning 'default'"):
                    connection.creation.clone_test_db(
                        suffix=str(index + 1),
                        verbosity=self.verbosity,
                        keepdb=self.keepdb,
                    )
        return old_config
