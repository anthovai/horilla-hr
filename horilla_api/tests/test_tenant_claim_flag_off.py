"""
With ORVAHR_MULTI_TENANT off the tenant-aware token classes behave exactly
like simplejwt's: no tenant claim is minted and none is required.
"""

import unittest

from django.conf import settings
from django.test import TestCase, override_settings
from rest_framework_simplejwt.tokens import AccessToken

from horilla_api.tokens import TENANT_CLAIM, TenantAccessToken, TenantRefreshToken
from horilla.testkit import make_user


# Flag-off behaviour only: in a SaaS-mode run the public schema has no user
# table, so this case cannot run there (orvahr_saas tests cover SaaS mode).
@unittest.skipIf(
    getattr(settings, "ORVAHR_MULTI_TENANT", False), "flag-off test; SaaS mode run"
)
@override_settings(ORVAHR_MULTI_TENANT=False)
class TenantClaimFlagOffTests(TestCase):
    def test_no_claim_and_plain_tokens_still_valid(self):
        user = make_user("flagoff")
        refresh = TenantRefreshToken.for_user(user)
        self.assertNotIn(TENANT_CLAIM, refresh.payload)
        self.assertNotIn(TENANT_CLAIM, refresh.access_token.payload)
        # Tokens minted by plain simplejwt (no claim) still validate.
        TenantAccessToken(str(AccessToken.for_user(user)))
        TenantRefreshToken(str(refresh))
