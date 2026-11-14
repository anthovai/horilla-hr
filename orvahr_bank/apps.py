"""Thai bank payroll transfer files."""

from django.apps import AppConfig


class OrvahrBankConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "orvahr_bank"
    verbose_name = "OrvaHR Bank Transfers"

    def ready(self):
        from django.urls import include, path

        from horilla.urls import urlpatterns

        urlpatterns.append(path("th-bank/", include("orvahr_bank.urls")))
        return super().ready()
