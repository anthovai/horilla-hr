"""Load or refresh the Thai provinces/districts/subdistricts. Safe to re-run."""

from django.core.management.base import BaseCommand

from orvahr_geo.loader import load_th_geo
from orvahr_geo.models import District, Province, Subdistrict


class Command(BaseCommand):
    help = "Load Thai provinces, districts and subdistricts (with postcodes)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--path", help="Fixture JSON (defaults to orvahr_geo/data/th_geo.json)."
        )

    def handle(self, *args, **options):
        result = load_th_geo(Province, District, Subdistrict, path=options["path"])
        for name, (created, updated, stale) in result.items():
            self.stdout.write(
                f"{name}: {created} created, {updated} updated, "
                f"{stale} not in fixture (kept)"
            )
