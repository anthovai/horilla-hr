from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class LineIntegrationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "line_integration"
    verbose_name = _("LINE")

    def ready(self):
        from django.urls import include, path

        from horilla.urls import urlpatterns

        # Same self-registration as whatsapp/apps.py: no edit to horilla/urls.py.
        urlpatterns.append(path("line/", include("line_integration.urls")))

        from line_integration import menu, signals  # noqa: F401

        signals.connect()
        super().ready()
