"""Tenant-side subscription and billing pages (active only in SaaS mode)."""

from django.apps import AppConfig


class OrvahrAccountConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "orvahr_account"
    verbose_name = "OrvaHR Account"
