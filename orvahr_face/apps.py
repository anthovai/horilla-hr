"""Server-side face check-in."""

from django.apps import AppConfig


class OrvahrFaceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "orvahr_face"
    verbose_name = "OrvaHR Face Check-in"

    def ready(self):
        from django.urls import include, path

        from horilla.urls import urlpatterns

        urlpatterns.append(path("face/", include("orvahr_face.urls")))
        return super().ready()
