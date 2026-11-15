"""Public landing page, sign-up form, sub-domain availability, anti-spam."""

import time

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.db import connection
from django.test import Client

from orvahr_saas.tests.base import SaasTestCase

HOST = "saas.test"


def signed_ts(seconds_ago=60):
    from orvahr_saas.views import _FORM_SALT

    return signing.TimestampSigner(salt=_FORM_SALT).sign(
        str(int(time.time()) - seconds_ago)
    )


class PublicSiteTestCase(SaasTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        settings.ORVAHR_SAAS_PUBLIC_HOSTS.append(HOST)
        settings.ALLOWED_HOSTS.append(HOST)
        self.web = Client(HTTP_HOST=HOST)

    def tearDown(self):
        settings.ORVAHR_SAAS_PUBLIC_HOSTS.remove(HOST)
        settings.ALLOWED_HOSTS.remove(HOST)
        connection.set_tenant(self.tenant)
        super().tearDown()

    def payload(self, **extra):
        from orvahr_saas.models import Plan

        data = {
            "company_name": "Siam Widgets Co., Ltd.",
            "slug": "siam-widgets",
            "contact_name": "Somsri Jaidee",
            "contact_email": "somsri@siamwidgets.test",
            "contact_phone": "081-234-5678",
            "employee_count": "42",
            "plan": str(Plan.objects.get(code="business").pk),
            "consent_to_contact": "on",
            "website": "",
            "ts": signed_ts(),
        }
        data.update(extra)
        return data

    def requests(self):
        from orvahr_saas.models import TenantRequest

        connection.set_schema_to_public()
        return TenantRequest.objects.all()


class LandingTests(PublicSiteTestCase):
    def test_landing_lists_public_plans_with_contact_us(self):
        response = self.web.get("/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Starter (sample)", body)
        self.assertIn("Enterprise (sample)", body)
        self.assertIn("Contact us", body)
        self.assertIn("/signup/", body)

    def test_landing_shows_prices_from_billing_provider(self):
        from orvahr_saas import plans as plans_mod

        old = plans_mod._price_provider
        plans_mod.register_price_provider(lambda ps: {"business": "฿4,900 / month"})
        try:
            body = self.web.get("/").content.decode()
        finally:
            plans_mod._price_provider = old
        self.assertIn("฿4,900 / month", body)

    def test_hidden_plan_not_listed(self):
        from orvahr_saas.models import Plan

        Plan.objects.filter(code="enterprise").update(is_public=False)
        self.assertNotIn("Enterprise (sample)", self.web.get("/").content.decode())

    def test_signup_page_renders_with_csrf_and_honeypot(self):
        response = self.web.get("/signup/?plan=starter")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("csrfmiddlewaretoken", body)
        self.assertIn('name="website"', body)

    def test_tenant_hosts_have_no_signup_or_console(self):
        tenant_client = Client(HTTP_HOST="alpha.saas.test")
        self.assertEqual(tenant_client.get("/signup/").status_code, 404)
        self.assertEqual(tenant_client.get("/console/").status_code, 404)


class AvailabilityTests(PublicSiteTestCase):
    def check(self, slug):
        return self.web.get("/signup/check/", {"slug": slug}).json()

    def test_free_name_is_available_with_domain(self):
        data = self.check("newco")
        self.assertTrue(data["available"])
        self.assertTrue(data["domain"].startswith("newco."))

    def test_existing_tenant_reserved_invalid_and_held_names(self):
        from orvahr_saas.models import TenantRequest

        self.assertFalse(self.check("t-alpha")["available"])
        for reserved in ("console", "admin", "www", "billing", "support"):
            self.assertFalse(self.check(reserved)["available"], reserved)
        self.assertFalse(self.check("Bad_Slug!")["available"])
        self.assertFalse(self.check("pg-thing")["available"])
        TenantRequest.objects.create(
            company_name="X",
            slug="held",
            contact_name="X",
            contact_email="x@x.test",
            contact_phone="0800000000",
            employee_count=3,
            consent_to_contact=True,
        )
        self.assertFalse(self.check("held")["available"])
        TenantRequest.objects.filter(slug="held").update(status="rejected")
        self.assertTrue(self.check("held")["available"])

    def test_check_is_rate_limited(self):
        from orvahr_saas.views import CHECK_LIMIT

        for _ in range(CHECK_LIMIT[0]):
            self.web.get("/signup/check/", {"slug": "abc"})
        self.assertEqual(
            self.web.get("/signup/check/", {"slug": "abc"}).status_code, 429
        )


class SignupTests(PublicSiteTestCase):
    def test_valid_request_is_stored_pending_and_nothing_provisioned(self):
        from orvahr_saas.models import Tenant

        tenants_before = Tenant.objects.count()
        response = self.web.post("/signup/", self.payload())
        self.assertRedirects(response, "/signup/thanks/", fetch_redirect_response=False)
        req = self.requests().get()
        self.assertEqual(req.status, "pending")
        self.assertEqual(req.slug, "siam-widgets")
        self.assertTrue(req.consent_to_contact)
        self.assertIsNotNone(req.consent_at)
        self.assertEqual(req.plan.code, "business")
        self.assertEqual(Tenant.objects.count(), tenants_before)
        self.assertEqual(self.web.get("/signup/thanks/").status_code, 200)

    def test_consent_is_required(self):
        data = self.payload()
        del data["consent_to_contact"]
        response = self.web.post("/signup/", data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.requests().exists())

    def test_reserved_taken_and_invalid_slugs_refused(self):
        for slug in ("console", "t-alpha", "1abc", "UPPER_case"):
            response = self.web.post("/signup/", self.payload(slug=slug))
            self.assertEqual(response.status_code, 200, slug)
        self.assertFalse(self.requests().exists())

    def test_second_request_for_same_slug_refused(self):
        self.web.post("/signup/", self.payload())
        response = self.web.post("/signup/", self.payload(contact_email="other@x.test"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("already taken", response.content.decode())
        self.assertEqual(self.requests().count(), 1)

    def test_bad_phone_and_count_refused(self):
        self.web.post("/signup/", self.payload(contact_phone="call me"))
        self.web.post("/signup/", self.payload(employee_count="0"))
        self.assertFalse(self.requests().exists())

    def test_honeypot_and_too_fast_look_successful_but_store_nothing(self):
        response = self.web.post("/signup/", self.payload(website="http://spam"))
        self.assertRedirects(response, "/signup/thanks/", fetch_redirect_response=False)
        response = self.web.post("/signup/", self.payload(ts=signed_ts(0)))
        self.assertRedirects(response, "/signup/thanks/", fetch_redirect_response=False)
        response = self.web.post("/signup/", self.payload(ts="forged"))
        self.assertRedirects(response, "/signup/thanks/", fetch_redirect_response=False)
        self.assertFalse(self.requests().exists())

    def test_posts_are_rate_limited(self):
        from orvahr_saas.views import SIGNUP_LIMIT

        for i in range(SIGNUP_LIMIT[0]):
            self.web.post("/signup/", self.payload(slug=f"co{i}x"))
        response = self.web.post("/signup/", self.payload(slug="late-one"))
        self.assertEqual(response.status_code, 429)
        self.assertFalse(self.requests().filter(slug="late-one").exists())
