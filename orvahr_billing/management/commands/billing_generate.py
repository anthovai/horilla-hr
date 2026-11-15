"""
Create the invoices for subscription periods that have started.

    python manage.py billing_generate [--date YYYY-MM-DD] [--draft] [--dry-run]

Run daily (cron / scheduler) on the public schema. Idempotent: a period is
invoiced once (unique per subscription + period start); running twice, or
two runs at once, creates nothing new.
"""

import datetime

from django.core.management.base import BaseCommand, CommandError

from orvahr_billing import services


class Command(BaseCommand):
    help = "Create invoices for subscriptions whose next period has started."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="Billing date (default: today), YYYY-MM-DD.")
        parser.add_argument(
            "--draft",
            action="store_true",
            help="Leave the invoices as drafts (no number).",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Show what would be invoiced."
        )

    def handle(self, *args, **options):
        on_date = None
        if options["date"]:
            try:
                on_date = datetime.date.fromisoformat(options["date"])
            except ValueError as exc:
                raise CommandError("--date must be YYYY-MM-DD") from exc
        results = services.generate_invoices(
            on_date,
            issue_now=not options["draft"],
            dry_run=options["dry_run"],
        )
        errors = 0
        for sub, period_start, outcome in results:
            if outcome.startswith("error"):
                errors += 1
            self.stdout.write(
                f"{sub.tenant.schema_name}\t{period_start or '-'}\t{outcome}"
            )
        self.stdout.write(f"{len(results)} period(s) processed, {errors} error(s).")
        if errors:
            raise CommandError(f"{errors} subscription(s) could not be invoiced")
