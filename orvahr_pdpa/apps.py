"""PDPA tools: consent, data subject requests, retention, sensitive-data access log."""

from django.apps import AppConfig
from django.db.models.signals import post_migrate
from django.utils.translation import gettext_lazy as _

OFFICER_GROUP = "PDPA Officer"
# Everything in this app except anonymisation (which also needs a superuser).
OFFICER_EXCLUDED = {"anonymise_personal_data"}


def ensure_officer_group(sender=None, **kwargs):
    """Create/refresh the "PDPA Officer" permission set after migrate."""
    from django.contrib.auth.models import Group, Permission

    from horilla.tenancy import app_has_tables

    # SaaS mode: the officer group belongs in each tenant schema, not public.
    if not app_has_tables("orvahr_pdpa"):
        return
    perms = Permission.objects.filter(content_type__app_label="orvahr_pdpa").exclude(
        codename__in=OFFICER_EXCLUDED
    )
    if not perms.exists():
        return
    group, _created = Group.objects.get_or_create(name=OFFICER_GROUP)
    group.permissions.add(*perms)


class OrvahrPdpaConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "orvahr_pdpa"
    verbose_name = _("PDPA")

    def ready(self):
        from django.urls import include, path

        from horilla.urls import urlpatterns

        urlpatterns.append(path("pdpa/", include("orvahr_pdpa.urls")))

        from orvahr_pdpa import menu  # noqa: F401

        post_migrate.connect(ensure_officer_group, sender=self, dispatch_uid="orvahr_pdpa_officer_group")
        return super().ready()
