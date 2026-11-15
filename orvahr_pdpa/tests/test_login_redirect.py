"""
The one-off privacy notice prompt after a browser login, and the way back
from it (``next`` on /pdpa/my-privacy/).
"""

from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

from django.test import Client
from django.urls import reverse

from orvahr_pdpa import consent
from orvahr_pdpa.models import ConsentRecord, PrivacyNotice
from orvahr_pdpa.tests.helpers import PdpaTestCase


class LoginPrivacyRedirectTests(PdpaTestCase):
    def setUp(self):
        super().setUp()
        self.emp = self.make_emp("login_worker")
        self.privacy_url = reverse("orvahr_pdpa:my-privacy")

    def publish(self):
        return PrivacyNotice.objects.create(
            audience="employee", version="1.0", title="Employee notice", text_th="ประกาศ",
            effective_from=date.today() - timedelta(days=1), published=True,
        )

    def login(self, username="login_worker", next_url=None, client=None):
        url = reverse("login")
        if next_url is not None:
            url += "?" + "next=" + next_url
        return (client or Client()).post(url, {"username": username, "password": "pass"})

    def assert_privacy_redirect(self, response, expected_next):
        self.assertEqual(response.status_code, 302)
        parsed = urlparse(response.url)
        self.assertEqual(parsed.path, self.privacy_url)
        self.assertEqual(parse_qs(parsed.query)["next"], [expected_next])

    def test_no_published_notice_logs_in_as_before(self):
        response = self.login()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/")

    def test_unacknowledged_notice_redirects_once_with_next(self):
        self.publish()
        self.assert_privacy_redirect(self.login(next_url="/employee/"), "/employee/")

    def test_unsafe_next_is_replaced(self):
        self.publish()
        self.assert_privacy_redirect(self.login(next_url="https://evil.example/"), "/")

    def test_acknowledged_notice_does_not_redirect(self):
        notice = self.publish()
        consent.acknowledge_notice(self.emp, notice)
        response = self.login(next_url="/employee/")
        self.assertEqual(response.url, "/employee/")

    def test_next_already_my_privacy_does_not_nest(self):
        self.publish()
        response = self.login(next_url=self.privacy_url)
        self.assertEqual(response.url, self.privacy_url)

    def test_superuser_is_prompted_too(self):
        self.make_emp("login_admin", is_superuser=True)
        self.publish()
        self.assert_privacy_redirect(self.login("login_admin"), "/")

    def test_my_privacy_offers_not_now_and_returns_to_next(self):
        notice = self.publish()
        client = self.client_for(self.emp)
        page = client.get(self.privacy_url, {"next": "/employee/"})
        self.assertContains(page, 'id="pdpa-not-now"')
        self.assertContains(page, 'name="next" value="/employee/"')

        response = client.post(self.privacy_url, {"action": "acknowledge", "next": "/employee/"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/employee/")
        self.assertTrue(
            ConsentRecord.all_objects.filter(employee=self.emp, notice=notice, action="acknowledged").exists()
        )
        # Acknowledged now, so the next login goes straight through.
        self.assertEqual(self.login(next_url="/employee/").url, "/employee/")

    def test_my_privacy_ignores_unsafe_next(self):
        self.publish()
        client = self.client_for(self.emp)
        page = client.get(self.privacy_url, {"next": "https://evil.example/"})
        self.assertNotContains(page, 'id="pdpa-not-now"')
        self.assertNotContains(page, 'name="next"')
        response = client.post(self.privacy_url, {"action": "acknowledge", "next": "//evil.example/"})
        self.assertEqual(response.url, self.privacy_url)
