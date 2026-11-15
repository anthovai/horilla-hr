"""
SaaS mode must not send ``SET search_path`` before every query.

django-tenants' default (TENANT_LIMIT_SET_CALLS off) re-issues the SET on
every cursor, doubling the database round trips of every request. The
setting in horilla/settings/saas.py limits it to once per tenant switch;
switching tenants must still re-issue it.
"""

import unittest

from django.conf import settings
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from base.models import Company


def _sets(captured):
    return [q for q in captured if q["sql"].startswith("SET search_path")]


@unittest.skipUnless(
    getattr(settings, "ORVAHR_MULTI_TENANT", False), "SaaS mode (schema per tenant) only"
)
class SearchPathSetOncePerTenantTests(TestCase):
    def test_consecutive_queries_share_one_set(self):
        with CaptureQueriesContext(connection) as ctx:
            for _ in range(3):
                Company.objects.count()
        self.assertLessEqual(len(_sets(ctx.captured_queries)), 1)

    def test_switching_tenant_sets_it_again(self):
        Company.objects.count()
        connection.set_tenant(connection.tenant)
        with CaptureQueriesContext(connection) as ctx:
            Company.objects.count()
        self.assertEqual(len(_sets(ctx.captured_queries)), 1)
