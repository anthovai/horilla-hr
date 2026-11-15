"""
The process-wide user -> accessibility-cache-key map must not mix tenants.

User ids repeat across tenant schemas. With a map keyed by the bare user id,
tenant A revoking a feature for its user 7 refreshed the session key of
tenant B's user 7 (the last one recorded) and left A's cached "allowed"
answer in place.
"""

from unittest import mock

from django.test import SimpleTestCase

from accessibility import middlewares
from accessibility.middlewares import ACCESSIBILITY_CACHE_USER_KEYS, user_cache_keys
from horilla.tenancy import schema_key


class AccessibilityCacheKeysPerTenantTests(SimpleTestCase):
    def setUp(self):
        self.saved = dict(ACCESSIBILITY_CACHE_USER_KEYS)
        ACCESSIBILITY_CACHE_USER_KEYS.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        ACCESSIBILITY_CACHE_USER_KEYS.clear()
        ACCESSIBILITY_CACHE_USER_KEYS.update(self.saved)

    def _in_schema(self, schema):
        return mock.patch("horilla.tenancy.current_schema", return_value=schema)

    def test_same_user_id_in_two_tenants_keeps_separate_keys(self):
        with self._in_schema("acme"):
            ACCESSIBILITY_CACHE_USER_KEYS.setdefault(schema_key(7), []).append(
                "acme-session"
            )
        with self._in_schema("globex"):
            self.assertEqual(user_cache_keys(7), [])
            ACCESSIBILITY_CACHE_USER_KEYS.setdefault(schema_key(7), []).append(
                "globex-session"
            )
        with self._in_schema("acme"):
            self.assertEqual(user_cache_keys(7), ["acme-session"])
        with self._in_schema("globex"):
            self.assertEqual(user_cache_keys(7), ["globex-session"])

    def test_single_tenant_mode_keys_by_bare_user_id(self):
        with self._in_schema(None):
            ACCESSIBILITY_CACHE_USER_KEYS.setdefault(schema_key(7), []).append("s")
        self.assertEqual(ACCESSIBILITY_CACHE_USER_KEYS, {7: ["s"]})
        with self._in_schema(None):
            self.assertEqual(middlewares.user_cache_keys(7), ["s"])
