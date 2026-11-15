"""
Shared fixtures for the SaaS-mode tests.

Creating a tenant schema runs every tenant migration (about two minutes on
a laptop), so the suite creates its tenants once and keeps them for the whole
run -- and across runs with ``--keepdb`` -- the same trade-off as
django-tenants' FastTenantTestCase, which ``TenantA`` builds on.

Tenants used everywhere:

=============  ==================  =========
schema         host                status
=============  ==================  =========
t_alpha        alpha.saas.test     active
t_beta         beta.saas.test      trial
=============  ==================  =========

Suspension is tested by flipping a status inside the test transaction
(``suspend()``), which is rolled back afterwards -- cheaper than a third
two-minute schema. (A tenant row without a schema is not an option: every
``migrate_schemas`` -- including the test runner's -- would fail on it.)
"""

import unittest

from django.conf import settings

if not getattr(settings, "ORVAHR_MULTI_TENANT", False):  # pragma: no cover
    raise unittest.SkipTest("SaaS-mode tests need ORVAHR_MULTI_TENANT=1 + PostgreSQL")

from django.db import connection  # noqa: E402
from django_tenants.test.cases import FastTenantTestCase  # noqa: E402

from orvahr_saas.models import Domain, Tenant  # noqa: E402

ALPHA = ("t_alpha", "alpha.saas.test", Tenant.STATUS_ACTIVE)
BETA = ("t_beta", "beta.saas.test", Tenant.STATUS_TRIAL)


def ensure_tenant(schema_name, domain, status):
    """Get or create a tenant (committed, outside any test transaction)."""
    connection.set_schema_to_public()
    tenant = Tenant.objects.filter(schema_name=schema_name).first()
    if tenant is None:
        tenant = Tenant(
            schema_name=schema_name,
            slug=schema_name.replace("_", "-"),
            name=schema_name.title(),
            status=status,
        )
        tenant.save(verbosity=0)
    elif tenant.status != status:
        Tenant.objects.filter(pk=tenant.pk).update(status=status)
        tenant.status = status
    Domain.objects.get_or_create(
        domain=domain, defaults={"tenant": tenant, "is_primary": True}
    )
    for host in (domain,):
        if host not in settings.ALLOWED_HOSTS and "*" not in settings.ALLOWED_HOSTS:
            settings.ALLOWED_HOSTS.append(host)
    return tenant


def all_tenants():
    """(alpha, beta) -- created on first use."""
    return ensure_tenant(*ALPHA), ensure_tenant(*BETA)


def suspend(tenant):
    """Mark ``tenant`` suspended for the rest of the current test (rolled back)."""
    Tenant.objects.filter(pk=tenant.pk).update(status=Tenant.STATUS_SUSPENDED)


class SaasTestCase(FastTenantTestCase):
    """
    TestCase whose connection starts on tenant alpha, with beta available as
    ``cls.beta``. Each test runs in a transaction that is rolled back,
    across every schema it touched.
    """

    @classmethod
    def get_test_schema_name(cls):
        return ALPHA[0]

    @classmethod
    def get_test_tenant_domain(cls):
        return ALPHA[1]

    @classmethod
    def setup_tenant(cls, tenant):
        tenant.slug = "t-alpha"
        tenant.name = "T_Alpha"
        tenant.status = ALPHA[2]

    @classmethod
    def setUpClass(cls):
        cls.alpha, cls.beta = all_tenants()
        super().setUpClass()

    def setUp(self):
        super().setUp()
        connection.set_tenant(self.tenant)
