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

        # Setup wizard: a new contract saved without a tax setting gets the
        # tax pack its company chose there.
        from django.db.models.signals import pre_save

        from orvahr_th.setup_wizard import default_contract_tax_pack

        pre_save.connect(
            default_contract_tax_pack,
            sender="payroll.Contract",
            dispatch_uid="orvahr_th_default_contract_tax_pack",
        )
        return super().ready()
