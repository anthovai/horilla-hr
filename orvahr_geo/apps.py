"""Thai provinces, districts, subdistricts and postcodes."""

from django.apps import AppConfig


class OrvahrGeoConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "orvahr_geo"
    verbose_name = "OrvaHR Thai Geography"

    def ready(self):
        from django.urls import include, path

        from horilla.urls import urlpatterns

        urlpatterns.append(path("th-geo/", include("orvahr_geo.urls")))
        return super().ready()
