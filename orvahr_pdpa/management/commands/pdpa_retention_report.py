"""
Dry-run retention report. Lists records past retention for each active
RetentionPolicy. This command never modifies or deletes anything; there is
no flag that makes it do so.
"""

from django.core.management.base import BaseCommand

from orvahr_pdpa.models import RetentionPolicy
from orvahr_pdpa.retention import dry_run_report


class Command(BaseCommand):
    help = "DRY RUN: list records past their retention period. Never deletes."

    def add_arguments(self, parser):
        parser.add_argument("--policy", type=int, help="Only this RetentionPolicy id")
        parser.add_argument("--sample", type=int, default=20, help="Record ids to print per policy")

    def handle(self, *args, **options):
        policies = RetentionPolicy.all_objects.filter(is_active=True)
        if options.get("policy"):
            policies = policies.filter(pk=options["policy"])
        self.stdout.write("DRY RUN - no records are changed or deleted.")
        for row in dry_run_report(policies, sample=options["sample"]):
            policy = row["policy"]
            header = (
                f"[{policy.pk}] {policy.name}: {policy.model_label}.{policy.date_field} "
                f"older than {row['cutoff']} ({policy.keep_years}y, action={policy.action})"
            )
            self.stdout.write(header)
            if row["error"]:
                self.stdout.write(self.style.ERROR(f"  misconfigured: {row['error']}"))
                continue
            self.stdout.write(f"  {row['count']} record(s) past retention")
            for pk, _label in row["sample"]:
                # Ids only: labels may contain personal data.
                self.stdout.write(f"  - id {pk}")
