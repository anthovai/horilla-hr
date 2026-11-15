"""
SaaS mode: ``migrate`` is django-tenants' ``migrate_schemas`` (public schema
first, then every tenant schema), carrying Horilla's autodetector so Django's
commands.E001 check (makemigrations and migrate must agree) still passes.
See horilla/__init__.py for the single-tenant patch this mirrors.
"""

from django_tenants.management.commands.migrate import Command as TenantMigrate

from horilla.inherit.autodetect import HorillaAutodetector


class Command(TenantMigrate):
    autodetector = HorillaAutodetector
