"""Thai statutory filings: PND 1 / PND 1 Kor, SSO 1-10, company tax registration data."""

from django.apps import AppConfig


class OrvahrFilingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "orvahr_filing"
    verbose_name = "OrvaHR Statutory Filings"

    def ready(self):
        from django.urls import include, path

        from horilla.urls import urlpatterns

        urlpatterns.append(path("th-filing/", include("orvahr_filing.urls")))
        return super().ready()
