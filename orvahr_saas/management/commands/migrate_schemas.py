"""``migrate_schemas`` with Horilla's autodetector (see ``migrate`` here)."""

from django_tenants.management.commands.migrate_schemas import Command as Base

from horilla.inherit.autodetect import HorillaAutodetector


class Command(Base):
    autodetector = HorillaAutodetector
