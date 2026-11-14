"""
manage.py sync_pvd_deductions [--date YYYY-MM-DD] [--tax-year YYYY]

Keeps the payroll Deductions for provident fund members in line with the
memberships in force on the date (default today); see
orvahr_benefits.services.pvd. With --tax-year it also upserts each member's
``provident_fund`` tax claim for that year from the issued payslips.

Prints counts only -- never names, IDs or amounts.
"""

import datetime

from django.core.management.base import BaseCommand, CommandError

from orvahr_benefits.services import pvd


class Command(BaseCommand):
    help = "Sync provident fund memberships to payroll deductions (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="Membership date, YYYY-MM-DD (default today).")
        parser.add_argument(
            "--tax-year", type=int, help="Also upsert provident_fund tax claims for this year."
        )

    def handle(self, *args, **options):
        on = None
        if options.get("date"):
            try:
                on = datetime.date.fromisoformat(options["date"])
            except ValueError as exc:
                raise CommandError("--date must be YYYY-MM-DD") from exc
        summary = pvd.sync_pvd_deductions(on)
        self.stdout.write(
            "PVD deductions: {created} created, {updated} updated, "
            "{membership_changes} membership changes, {members} members.".format(**summary)
        )
        year = options.get("tax_year")
        if year:
            if not 2000 <= year <= 2200:
                raise CommandError("--tax-year out of range")
            claims = pvd.upsert_pvd_tax_claims(year)
            self.stdout.write(
                "Tax claims {year}: {created} created, {updated} updated, "
                "{unchanged} unchanged.".format(year=year, **claims)
            )
