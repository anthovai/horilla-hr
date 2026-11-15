"""JWT tokens are bound to the tenant that issued them (horilla_api/tokens.py)."""

import jwt
from django.conf import settings
from django.db import connection
from django.test import Client
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import AccessToken

from horilla_api.tokens import TENANT_CLAIM, TenantAccessToken, TenantRefreshToken
from orvahr_saas.tests.base import SaasTestCase

PASSWORD = "Jwt-Tenant-Pass-1!"
ALPHA_HOST = "alpha.saas.test"
BETA_HOST = "beta.saas.test"
REJECTED = b"not valid for this organisation"


def claims(token):
    return jwt.decode(token, options={"verify_signature": False})


class TenantJwtTests(SaasTestCase):
    def setUp(self):
        super().setUp()
        from horilla.testkit import make_company, make_employee, make_user

        company = make_company("Alpha JWT Co")
        self.user = make_user("jwt-admin", password=PASSWORD, is_superuser=True)
        make_employee(company=company, email="jwt-admin@alpha.test", user=self.user)
        self.client = Client()

    def _login(self, host=ALPHA_HOST):
        response = self.client.post(
            "/api/auth/login/",
            {"username": "jwt-admin", "password": PASSWORD},
            content_type="application/json",
            HTTP_HOST=host,
        )
        connection.set_tenant(self.tenant)
        return response

    def _get(self, host, access):
        response = self.client.get(
            "/api/base/capabilities/",
            HTTP_HOST=host,
            HTTP_AUTHORIZATION=f"Bearer {access}",
        )
        connection.set_tenant(self.tenant)
        return response

    def test_settings_validate_with_tenant_token_class(self):
        self.assertEqual(
            settings.SIMPLE_JWT["AUTH_TOKEN_CLASSES"],
            ("horilla_api.tokens.TenantAccessToken",),
        )

    def test_login_tokens_carry_the_tenant_claim(self):
        response = self._login()
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(claims(body["access"])[TENANT_CLAIM], "t_alpha")
        self.assertEqual(claims(body["refresh"])[TENANT_CLAIM], "t_alpha")
        self.assertEqual(self._get(ALPHA_HOST, body["access"]).status_code, 200)

    def test_token_from_alpha_is_rejected_on_beta_host(self):
        access = self._login().json()["access"]
        response = self._get(BETA_HOST, access)
        self.assertEqual(response.status_code, 401)
        self.assertIn(REJECTED, response.content)

    def test_token_without_claim_is_rejected(self):
        legacy = str(AccessToken.for_user(self.user))  # plain simplejwt token
        self.assertNotIn(TENANT_CLAIM, claims(legacy))
        response = self._get(ALPHA_HOST, legacy)
        self.assertEqual(response.status_code, 401)
        self.assertIn(REJECTED, response.content)

    def test_refresh_keeps_the_claim_and_is_tenant_bound(self):
        refresh = self._login().json()["refresh"]
        # Another tenant's host refuses the refresh token outright.
        response = self.client.post(
            "/api/auth/refresh/",
            {"refresh": refresh},
            content_type="application/json",
            HTTP_HOST=BETA_HOST,
        )
        self.assertEqual(response.status_code, 401)
        self.assertIn(REJECTED, response.content)

        response = self.client.post(
            "/api/auth/refresh/",
            {"refresh": refresh},
            content_type="application/json",
            HTTP_HOST=ALPHA_HOST,
        )
        connection.set_tenant(self.tenant)
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(claims(body["access"])[TENANT_CLAIM], "t_alpha")
        self.assertEqual(claims(body["refresh"])[TENANT_CLAIM], "t_alpha")
        self.assertEqual(self._get(ALPHA_HOST, body["access"]).status_code, 200)
        self.assertEqual(self._get(BETA_HOST, body["access"]).status_code, 401)

    def test_logout_refuses_another_tenants_refresh_token(self):
        refresh = self._login().json()["refresh"]
        response = self.client.post(
            "/api/auth/logout/",
            {"refresh": refresh},
            content_type="application/json",
            HTTP_HOST=BETA_HOST,
        )
        connection.set_tenant(self.tenant)
        self.assertEqual(response.status_code, 401)

    def test_token_classes_directly(self):
        refresh = TenantRefreshToken.for_user(self.user)
        self.assertEqual(refresh[TENANT_CLAIM], "t_alpha")
        access = str(refresh.access_token)
        TenantAccessToken(access)  # valid on alpha
        connection.set_tenant(self.beta)
        with self.assertRaises(TokenError):
            TenantAccessToken(access)
        connection.set_schema_to_public()
        with self.assertRaises(TokenError):
            TenantAccessToken(access)
