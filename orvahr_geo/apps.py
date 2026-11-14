"""Thai provinces, districts, subdistricts and postcodes."""

from django.apps import AppConfig


class OrvahrGeoConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "orvahr_geo"
    verbose_name = "OrvaHR Thai Geography"

    def ready(self):
        from django.urls import clear_url_caches, include, path

        from horilla.urls import urlpatterns

        urlpatterns.append(path("th-geo/", include("orvahr_geo.urls")))
        # An earlier app's ready() may already have resolved a URL, which
        # freezes the resolver's namespace table without "orvahr_geo".
        clear_url_caches()
        return super().ready()
