from django.apps import AppConfig
from django.conf import settings


class OrvahrSaasConfig(AppConfig):
    """
    SaaS control-plane app: the tenant registry (one PostgreSQL schema per
    customer). Installed only when ORVAHR_MULTI_TENANT is on, and only in the
    public schema (SHARED_APPS). See horilla/settings/saas.py.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "orvahr_saas"
    verbose_name = "OrvaHR SaaS"

    def ready(self):
        # Some apps join INSTALLED_APPS while the registry is loading
        # (horilla_api/__init__.py appends geofencing and facedetection), after
        # settings computed TENANT_APPS. Every installed app that is not
        # public-only is a tenant app, so add the latecomers here -- ready()
        # runs once the registry is complete and before any migrate.
        from django.apps import apps

        from horilla.settings.saas import SAAS_PUBLIC_ONLY_APPS

        tenant_apps = settings.TENANT_APPS
        for config in apps.get_app_configs():
            if (
                config.name not in tenant_apps
                and config.name not in SAAS_PUBLIC_ONLY_APPS
            ):
                tenant_apps.append(config.name)
