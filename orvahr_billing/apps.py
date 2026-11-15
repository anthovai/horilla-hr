"""SaaS billing. Installed only when ORVAHR_MULTI_TENANT is on (see horilla/settings/saas.py)."""

from django.apps import AppConfig


class OrvahrBillingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "orvahr_billing"
    verbose_name = "OrvaHR Billing"
