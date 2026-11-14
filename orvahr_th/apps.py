"""OrvaHR Thai localisation: formatting, Thai PDF font, 50 ทวิ draft, certificates."""

from django.apps import AppConfig


class OrvahrThConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "orvahr_th"
    verbose_name = "OrvaHR Thailand"

    def ready(self):
        from django.urls import include, path

        from horilla.urls import urlpatterns

        urlpatterns.append(path("orvahr-th/", include("orvahr_th.urls")))

        from orvahr_th import sidebar  # noqa: F401  (adds a submenu)
        return super().ready()
