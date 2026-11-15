"""
JWT token classes that carry the tenant (SaaS mode).

In SaaS mode every tenant is its own PostgreSQL schema with its own users
table, so ``user_id: 7`` means a different person on every host. A token
minted on ``acme.<base>`` must therefore never authenticate on
``globex.<base>``. Before this, the only thing stopping that replay was
``CHECK_REVOKE_TOKEN`` (the other tenant's user 7 has a different password
hash) -- an accident, not a control.

* Every token *minted* while a tenant is active gets the claim
  ``TENANT_CLAIM`` = the active schema name. Access tokens derived from a
  refresh token copy it (simplejwt copies every claim but exp/jti/iat/type),
  and a rotated refresh token keeps its payload, so the claim survives
  ``/auth/refresh/``.
* Every token *verified* while a tenant is active must carry the claim and it
  must equal the active schema; otherwise ``TokenError`` -> 401. Tokens
  without the claim (issued before SaaS mode, or by another code path) are
  refused in SaaS mode.

With ``ORVAHR_MULTI_TENANT`` off both classes behave exactly like
simplejwt's own: no claim is added and nothing extra is checked.

Wiring: in SaaS mode ``horilla/settings/saas.py`` sets
``SIMPLE_JWT["AUTH_TOKEN_CLASSES"]`` to ``TenantAccessToken``, so every
``JWTAuthentication`` (the DRF default class, the Swagger fallback,
``base.views.is_jwt_token_valid``) checks the claim;
``TenantScopedJWTAuthentication`` re-checks it explicitly; the login view
mints ``TenantRefreshToken`` and the refresh/logout serializers decode with it.
"""

from __future__ import annotations

from django.utils.translation import gettext_lazy as _
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from horilla.tenancy import current_schema, is_multi_tenant, public_schema_name

TENANT_CLAIM = "tenant_schema"


def _active_schema():
    """The tenant schema tokens are bound to, or None (flag off / public)."""
    if not is_multi_tenant():
        return None
    schema = current_schema()
    if not schema or schema == public_schema_name():
        return None
    return schema


def check_token_tenant(payload) -> None:
    """
    Raise ``TokenError`` unless ``payload`` belongs to the active tenant.
    No-op with the flag off. On the public schema (no tenant) every token is
    refused: no API is served there, and a token can't belong to "nobody".
    """
    if not is_multi_tenant():
        return
    expected = _active_schema()
    claim = payload.get(TENANT_CLAIM)
    if expected is None or claim is None or claim != expected:
        raise TokenError(_("Token is not valid for this organisation."))


class _TenantClaimMixin:
    def __init__(self, token=None, verify=True):
        super().__init__(token, verify=verify)
        if token is None:
            # Newly minted (for_user, access_token, ...): bind to the tenant.
            schema = _active_schema()
            if schema is not None:
                self.payload[TENANT_CLAIM] = schema

    def verify(self, *args, **kwargs):
        super().verify(*args, **kwargs)
        check_token_tenant(self.payload)


class TenantAccessToken(_TenantClaimMixin, AccessToken):
    pass


class TenantRefreshToken(_TenantClaimMixin, RefreshToken):
    access_token_class = TenantAccessToken
