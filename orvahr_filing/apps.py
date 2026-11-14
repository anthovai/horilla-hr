from django.apps import AppConfig


class OrvahrFilingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "orvahr_filing"
    verbose_name = "OrvaHR Statutory Filings"

    def ready(self):
        from django.urls import include, path

        from horilla.urls import urlpatterns

        urlpatterns.append(path("th-filing/", include("orvahr_filing.urls")))

        # No reverse()/resolve() and no import of a module that does one
        # (payroll.sidebar) here: see orvahr_filing.menu.register_payroll_submenu.
        from orvahr_filing import menu

        menu.connect()
        return super().ready()
