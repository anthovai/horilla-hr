"""Server-side face check-in."""

from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class OrvahrFaceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "orvahr_face"
    verbose_name = _("Face Check-in")

    def ready(self):
        from django.urls import include, path

        from horilla.urls import urlpatterns

        urlpatterns.append(path("face/", include("orvahr_face.urls")))

        from orvahr_face import menu, scheduler, signals  # noqa: F401

        signals.connect_consent_signal()
        return super().ready()
