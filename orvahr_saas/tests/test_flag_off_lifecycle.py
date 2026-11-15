"""
ORVAHR_MULTI_TENANT off: notifications, the lifecycle run and the billing
hook are no-ops (SimpleTestCase: any query would fail the test).
"""

import unittest

from django.conf import settings
from django.core import mail
from django.test import SimpleTestCase

if getattr(settings, "ORVAHR_MULTI_TENANT", False):  # pragma: no cover
    raise unittest.SkipTest("flag-off proofs; SaaS mode has its own tests")


class Dummy:
    pk = 1
    name = "X"
    contact_email = "x@example.test"


class FlagOffLifecycleTests(SimpleTestCase):
    def test_send_is_a_noop(self):
        from orvahr_saas import notifications

        mail.outbox = []
        self.assertEqual(
            notifications.send(
                "tenant_suspended",
                to=["x@example.test"],
                audience="tenant",
                context={},
                language="th",
            ),
            [],
        )
        self.assertEqual(mail.outbox, [])

    def test_lifecycle_is_a_noop(self):
        from orvahr_saas.lifecycle import daily_lifecycle, run_lifecycle

        self.assertEqual(run_lifecycle(), [])
        self.assertEqual(run_lifecycle(dry_run=True), [])
        daily_lifecycle()

    def test_middleware_helper_needs_no_db(self):
        from orvahr_saas.middleware import allowed_when_locked

        self.assertTrue(allowed_when_locked("/login/"))
        self.assertTrue(allowed_when_locked("/login"))
        self.assertTrue(allowed_when_locked("/subscription/documents/"))
        self.assertFalse(allowed_when_locked("/"))
        self.assertFalse(allowed_when_locked("/loginx/"))
        self.assertFalse(allowed_when_locked("/employee/"))
