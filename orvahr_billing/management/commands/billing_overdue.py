"""
Daily overdue check.

    python manage.py billing_overdue [--date YYYY-MM-DD] [--grace-days N]
                                     [--dry-run] [--suspend]

Marks issued, unpaid invoices past their due date as overdue. Tenants with
an invoice overdue for more than the grace period (default
ORVAHR_BILLING_SUSPEND_GRACE_DAYS, 14) are listed; they are suspended (via
orvahr_saas.services.suspend_tenant) ONLY when ORVAHR_BILLING_AUTO_SUSPEND
is true in settings -- or ``--suspend`` is given -- and never with
``--dry-run``.
"""

import datetime

from django.core.management.base import BaseCommand, CommandError

from orvahr_billing import services


class Command(BaseCommand):
    help = "Mark overdue invoices; optionally suspend tenants past the grace period."

    def add_arguments(self, parser):
        parser.add_argument("--date", help="Check date (default: today), YYYY-MM-DD.")
        parser.add_argument(
            "--grace-days", type=int, help="Days after the due date before suspension."
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Change nothing; report only."
        )
        parser.add_argument(
            "--suspend",
            action="store_true",
            help="Suspend even if ORVAHR_BILLING_AUTO_SUSPEND is off (explicit operator run).",
        )

    def handle(self, *args, **options):
        on_date = None
        if options["date"]:
            try:
                on_date = datetime.date.fromisoformat(options["date"])
            except ValueError as exc:
                raise CommandError("--date must be YYYY-MM-DD") from exc
        allow = True if options["suspend"] else None
        report = services.process_overdue(
            on_date,
            grace_days=options["grace_days"],
            dry_run=options["dry_run"],
            allow_suspend=allow,
        )
        prefix = "[dry-run] " if options["dry_run"] else ""
        for invoice in report["marked"]:
            self.stdout.write(
                f"{prefix}overdue\t{invoice.number}\t{invoice.tenant.schema_name}\tdue {invoice.due_date}"
            )
        for tenant, invoice, outcome in report["suspend"]:
            self.stdout.write(
                f"{prefix}past grace\t{tenant.schema_name}\t{invoice.number}\t{outcome}"
            )
        self.stdout.write(
            f"{prefix}{len(report['marked'])} newly overdue, {len(report['suspend'])} tenant(s) past the grace period."
        )
