"""base.views.EnableIntegrationsView: allow-listed apps and per-app permission."""

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from base.models import IntegrationApps
from horilla.testkit import make_company, make_employee, make_user


class EnableIntegrationTests(TestCase):
    def setUp(self):
        self.company = make_company("Toggle Co")
        self.user = make_user("toggler")
        make_employee(company=self.company, email="toggler@test.horilla", user=self.user)
        self.client.force_login(self.user)

    def superuser(self, username):
        user = make_user(username, is_superuser=True)
        make_employee(company=self.company, email=f"{username}@test.horilla", user=user)
        return user

    def grant(self, codename):
        app_label, code = codename.split(".")
        self.user.user_permissions.add(
            Permission.objects.get(content_type__app_label=app_label, codename=code)
        )

    def post(self, app_label, enabled=True):
        data = {"is_enabled": "on"} if enabled else {}
        return self.client.post(
            reverse("enable-integration") + f"?app_label={app_label}", data
        )

    def enabled(self, app_label):
        return IntegrationApps.objects.filter(app_label=app_label, is_enabled=True).exists()

    def test_allowed_with_app_change_permission(self):
        self.grant("line_integration.change_linechannel")
        response = self.post("line_integration")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.enabled("line_integration"))
        response = self.post("line_integration", enabled=False)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.enabled("line_integration"))

    def test_denied_without_permission(self):
        self.assertEqual(self.post("whatsapp").status_code, 403)
        self.assertFalse(IntegrationApps.objects.filter(app_label="whatsapp").exists())

    def test_old_gate_permission_no_longer_enough_for_other_apps(self):
        # horilla_meet.view_googlecloudcredential used to open every toggle.
        self.grant("horilla_meet.view_googlecloudcredential")
        self.assertEqual(self.post("whatsapp").status_code, 403)
        self.assertEqual(self.post("horilla_meet").status_code, 403)

    def test_permission_for_one_app_does_not_open_another(self):
        self.grant("whatsapp.change_whatsappcredientials")
        self.assertEqual(self.post("line_integration").status_code, 403)
        self.assertEqual(self.post("whatsapp").status_code, 200)
        self.assertTrue(self.enabled("whatsapp"))

    def test_unknown_label_rejected_even_for_superuser(self):
        self.client.force_login(self.superuser("root"))
        self.assertEqual(self.post("payroll").status_code, 403)
        self.assertEqual(self.post("not_an_app").status_code, 403)
        self.assertFalse(IntegrationApps.objects.filter(app_label="payroll").exists())

    def test_superuser_allowed_for_known_label(self):
        self.client.force_login(self.superuser("root2"))
        self.assertEqual(self.post("linkedin").status_code, 200)
        self.assertTrue(self.enabled("linkedin"))

    def test_anonymous_redirected(self):
        self.client.logout()
        response = self.post("whatsapp")
        self.assertEqual(response.status_code, 302)
