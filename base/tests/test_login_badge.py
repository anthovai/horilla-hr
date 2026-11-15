"""Login accepts an employee badge ID as well as the account username."""

from django.test import TestCase
from django.urls import reverse

from employee.models import Employee
from horilla.testkit import make_company, make_employee, make_user


class BadgeLoginTests(TestCase):
    def setUp(self):
        company = make_company("Badge Login Co")
        self.user = make_user("badge.user@test.horilla", password="pw-12345")
        self.employee = make_employee(
            company=company, email="badge.user@test.horilla", user=self.user
        )
        Employee.objects.filter(pk=self.employee.pk).update(badge_id="BADGE0001")
        self.url = reverse("login")

    def _login(self, username, password="pw-12345"):
        return self.client.post(self.url, {"username": username, "password": password})

    def _logged_in(self):
        return "_auth_user_id" in self.client.session

    def test_badge_id_logs_in(self):
        self._login("BADGE0001")
        self.assertTrue(self._logged_in())

    def test_username_still_logs_in(self):
        self._login("badge.user@test.horilla")
        self.assertTrue(self._logged_in())

    def test_wrong_password_with_badge_id_is_rejected(self):
        self._login("BADGE0001", "nope")
        self.assertFalse(self._logged_in())

    def test_unknown_badge_is_rejected(self):
        self._login("BADGE9999")
        self.assertFalse(self._logged_in())
