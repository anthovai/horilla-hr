"""PDPA tools: consent, data subject requests, retention, sensitive-data access log."""

from django.apps import AppConfig


class OrvahrPdpaConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "orvahr_pdpa"
    verbose_name = "OrvaHR PDPA"

    def ready(self):
        from django.urls import include, path

        from horilla.urls import urlpatterns

        urlpatterns.append(path("pdpa/", include("orvahr_pdpa.urls")))
        return super().ready()
