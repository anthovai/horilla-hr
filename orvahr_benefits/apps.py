"""Provident fund, severance and final settlement, student-loan (กยศ.) and court garnishment deductions."""

from django.apps import AppConfig


class OrvahrBenefitsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "orvahr_benefits"
    verbose_name = "OrvaHR Thai Benefits"

    def ready(self):
        from django.urls import include, path

        from horilla.urls import urlpatterns

        urlpatterns.append(path("th-benefits/", include("orvahr_benefits.urls")))
        return super().ready()
