"""
Report active contracts paying less than the provincial minimum daily wage.

    manage.py contracts_below_minimum_wage [--as-of YYYY-MM-DD]
        [--days-per-month 30] [--hours-per-day 8] [--company ID]
        [--show-amounts] [--all]

Read-only. Rates come from MinimumWageRate (load them first with
load_th_minimum_wage); how each contract's work province is found, and how a
monthly or hourly wage is turned into a daily figure, is described in
payroll/methods/minimum_wage.py. In short: the employee's Work Location when it
names a province with a rate, otherwise the company address State.

Pay figures are left out of the output unless --show-amounts is given, so the
default report can be run from cron or pasted into a ticket without putting
anyone's pay in a log. Employees are named by badge and name only.

Exits non-zero when anything is below the minimum, so it can gate a run.
"""

import datetime

from django.core.management.base import BaseCommand, CommandError

from payroll.methods.minimum_wage import (
    DEFAULT_DAYS_PER_MONTH,
    DEFAULT_HOURS_PER_DAY,
    SOURCE_COMPANY_STATE,
    check_contracts,
)

SOURCE_LABELS = {
    "work_location": "work location",
    SOURCE_COMPANY_STATE: "company address (assumed)",
    "": "not recorded",
}


class Command(BaseCommand):
    help = "Report active contracts below the provincial minimum daily wage."

    def add_arguments(self, parser):
        parser.add_argument("--as-of", help="Date the rates are taken at (default today).")
        parser.add_argument(
            "--days-per-month",
            type=float,
            default=DEFAULT_DAYS_PER_MONTH,
            help="What a monthly wage is divided by (default 30; verify against current law).",
        )
        parser.add_argument(
            "--hours-per-day",
            type=float,
            default=DEFAULT_HOURS_PER_DAY,
            help="What an hourly rate is multiplied by (default 8).",
        )
        parser.add_argument("--company", type=int, help="Limit to one company id.")
        parser.add_argument(
            "--show-amounts",
            action="store_true",
            help="Include the daily equivalent and the shortfall in the output.",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Also list contracts with no rate found for their province.",
        )

    def handle(self, *args, **options):
        as_of = None
        if options["as_of"]:
            try:
                as_of = datetime.date.fromisoformat(options["as_of"])
            except ValueError as exc:
                raise CommandError("--as-of must be YYYY-MM-DD") from exc
        if options["days_per_month"] <= 0 or options["hours_per_day"] <= 0:
            raise CommandError("--days-per-month and --hours-per-day must be positive")

        findings = check_contracts(
            as_of=as_of,
            days_per_month=options["days_per_month"],
            hours_per_day=options["hours_per_day"],
            company=options["company"],
        )
        below = [f for f in findings if f.below]
        unresolved = [f for f in findings if f.rate is None]
        assumed = [f for f in findings if f.source == SOURCE_COMPANY_STATE]

        for finding in below:
            employee = finding.contract.employee_id
            line = (
                f"  BELOW  {employee.badge_id or '-'} {employee.get_full_name()} — "
                f"{finding.contract.contract_name} ({finding.contract.wage_type}); "
                f"{finding.rate.province_code} {finding.rate.province_name} from "
                f"{SOURCE_LABELS[finding.source]}, minimum {finding.rate.daily_amount}/day"
            )
            if options["show_amounts"]:
                line += (
                    f", pays {finding.daily_equivalent}/day, "
                    f"short {finding.shortfall}/day"
                )
            self.stdout.write(line)

        if options["all"]:
            for finding in unresolved:
                employee = finding.contract.employee_id
                where = finding.province or "no province recorded"
                self.stdout.write(
                    f"  NO RATE  {employee.badge_id or '-'} {employee.get_full_name()} — "
                    f"{finding.contract.contract_name}; {where} "
                    f"({SOURCE_LABELS[finding.source]})"
                )

        self.stdout.write(
            f"{len(findings)} active contract(s) checked: {len(below)} below the "
            f"minimum, {len(unresolved)} with no rate found for their province, "
            f"{len(assumed)} placed by company address."
        )
        if assumed:
            self.stdout.write(
                "The company address is used where no work location names a "
                "province; that is only right for staff who work at that address."
            )
        self.stdout.write(
            "Rates are only as good as the loaded file — verify them against "
            "the current official announcement."
        )
        if below:
            raise SystemExit(1)
