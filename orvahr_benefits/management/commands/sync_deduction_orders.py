"""
manage.py sync_deduction_orders [--date YYYY-MM-DD]

Keeps one payroll Deduction per student-loan / garnishment order in line with
the order (in force on the date, remaining balance); see
orvahr_benefits.services.orders. Prints counts only.
"""

import datetime

from django.core.management.base import BaseCommand, CommandError

from orvahr_benefits.services import orders


class Command(BaseCommand):
    help = "Sync student loan and garnishment orders to payroll deductions (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="Date the orders are applied as of (default today).")

    def handle(self, *args, **options):
        on = None
        if options.get("date"):
            try:
                on = datetime.date.fromisoformat(options["date"])
            except ValueError as exc:
                raise CommandError("--date must be YYYY-MM-DD") from exc
        summary = orders.sync_deduction_orders(on)
        parts = ", ".join(f"{count} {name}" for name, count in sorted(summary.items()))
        self.stdout.write(f"Deduction orders: {parts or 'none'}.")
