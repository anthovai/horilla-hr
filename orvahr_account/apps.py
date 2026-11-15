"""Tenant-side subscription and billing pages (active only in SaaS mode)."""

from django.apps import AppConfig
from django.conf import settings
from django.utils.translation import gettext_lazy as _

URL_PREFIX = "subscription/"


class OrvahrAccountConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "orvahr_account"
    verbose_name = _("Subscription")

    def ready(self):
        # Installed in both modes (so the tables and the permission exist and
        # migrations stay identical), but the pages only make sense with a
        # tenant and orvahr_billing behind them: mount nothing otherwise.
        if not getattr(settings, "ORVAHR_MULTI_TENANT", False):
            return super().ready()

        from django.urls import include, path

        from horilla.urls import urlpatterns

        urlpatterns.append(path(URL_PREFIX, include("orvahr_account.urls")))

        # Roles UI (group / employee permission matrix): show the
        # "Manage subscription and billing" permission, carried by
        # PaymentNotice; the request model's CRUD permissions are noise.
        if "orvahr_account" not in settings.APPS:
            settings.APPS.append("orvahr_account")
        if "planchangerequest" not in settings.NO_PERMISSION_MODALS:
            settings.NO_PERMISSION_MODALS.append("planchangerequest")
        return super().ready()
