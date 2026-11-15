from django.apps import AppConfig
from django.conf import settings


class HorillaLdapConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "horilla_ldap"
    verbose_name = "LDAP"

    def ready(self):
        from django.urls import include, path

        from horilla import config
        from horilla.urls import urlpatterns

        settings.APPS.append("horilla_ldap")
        urlpatterns.append(
            path("", include("horilla_ldap.urls")),
        )
        super().ready()

        from django.core import checks

        from horilla.tenancy import is_multi_tenant

        checks.register(check_ldap_backend_not_in_saas_mode)
        if is_multi_tenant():
            # LDAPSettings is a per-tenant table, but settings.AUTH_LDAP_* is
            # process-wide: copying one tenant's directory (bind DN and
            # password included) into it would hand it to every tenant. In
            # SaaS mode LDAP *sign-in* is therefore unsupported (see the check
            # below); the per-tenant LDAPSettings row is still used by the
            # import commands, which read it inside the tenant's schema
            # (``tenant_command import_ldap_users -s <schema>``).
            return

        ldap_config = config.load_ldap_settings()

        # Apply settings dynamically
        settings.LDAP_SERVER = ldap_config["LDAP_SERVER"]
        settings.BIND_DN = ldap_config["BIND_DN"]
        settings.BIND_PASSWORD = ldap_config["BIND_PASSWORD"]
        settings.BASE_DN = ldap_config["BASE_DN"]

        settings.AUTH_LDAP_SERVER_URI = settings.LDAP_SERVER
        settings.AUTH_LDAP_BIND_DN = settings.BIND_DN
        settings.AUTH_LDAP_BIND_PASSWORD = settings.BIND_PASSWORD
        settings.AUTH_LDAP_USER_SEARCH_BASE = settings.BASE_DN


LDAP_BACKEND = "django_auth_ldap.backend.LDAPBackend"


def check_ldap_backend_not_in_saas_mode(app_configs=None, **kwargs):
    """
    SaaS mode refuses to start with the LDAP login backend enabled: it would
    authenticate every tenant against one process-wide directory
    (settings.AUTH_LDAP_*), not the tenant's own LDAPSettings.
    """
    from django.core import checks

    from horilla.tenancy import is_multi_tenant

    if is_multi_tenant() and any(
        backend == LDAP_BACKEND or backend.endswith(".LDAPBackend")
        for backend in getattr(settings, "AUTHENTICATION_BACKENDS", ())
    ):
        return [
            checks.Error(
                "LDAP sign-in is not supported in SaaS mode "
                "(ORVAHR_MULTI_TENANT=1).",
                hint=(
                    "Remove the LDAP backend from AUTHENTICATION_BACKENDS. "
                    "LDAP settings are per tenant; only the import commands "
                    "use them. See orvahr_saas/README.md."
                ),
                id="orvahr_saas.E101",
            )
        ]
    return []
