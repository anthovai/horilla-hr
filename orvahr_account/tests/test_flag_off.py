"""
ORVAHR_MULTI_TENANT off (the default): orvahr_account is installed (its
tables exist) but no URL is mounted and the menu link is not rendered.
"""

import unittest

from django.conf import settings
from django.test import TestCase
from django.urls import NoReverseMatch, reverse

if getattr(settings, "ORVAHR_MULTI_TENANT", False):  # pragma: no cover
    raise unittest.SkipTest("flag-off proofs; SaaS mode has its own tests")


class FlagOffTests(TestCase):
    def test_app_installed_but_urls_not_mounted(self):
        self.assertIn("orvahr_account", settings.INSTALLED_APPS)
        self.assertNotIn("orvahr_account", settings.APPS)
        with self.assertRaises(NoReverseMatch):
            reverse("orvahr_account:overview")
        for path in ("/subscription/", "/subscription/documents/", "/subscription/plan/"):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_menu_link_not_rendered(self):
        from django.template import Context, Template

        html = Template(
            "{% url 'orvahr_account:overview' as u %}{% if u %}LINK{% endif %}"
        ).render(Context({}))
        self.assertEqual(html, "")

    def test_models_work_without_billing(self):
        from orvahr_account.models import PlanChangeRequest

        PlanChangeRequest.objects.create(requested_plan_code="business")
        self.assertEqual(PlanChangeRequest.objects.count(), 1)
