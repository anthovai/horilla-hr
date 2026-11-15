"""
Trial lifecycle: reminders before a trial ends, ``trial_expired`` after.

    python manage.py saas_lifecycle [--date 2026-12-01] [--dry-run]

Runs on the public schema (also scheduled daily by run_scheduler). Never
deletes anything. ``--dry-run`` prints what would happen and changes and
sends nothing.
"""

import datetime

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Send trial reminders and mark ended trials (SaaS mode)."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="Run as if today were YYYY-MM-DD.")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        if not getattr(settings, "ORVAHR_MULTI_TENANT", False):
            raise CommandError("saas_lifecycle needs ORVAHR_MULTI_TENANT=1.")
        on_date = None
        if options["date"]:
            try:
                on_date = datetime.date.fromisoformat(options["date"])
            except ValueError as exc:
                raise CommandError(f"Invalid --date: {exc}") from exc
        from orvahr_saas.lifecycle import run_lifecycle

        items = run_lifecycle(on_date, dry_run=options["dry_run"])
        prefix = "[dry-run] " if options["dry_run"] else ""
        for item in items:
            self.stdout.write(f"{prefix}{item}")
        self.stdout.write(f"{prefix}{len(items)} tenant(s) considered.")
