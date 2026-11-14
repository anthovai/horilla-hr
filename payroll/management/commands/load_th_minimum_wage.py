"""
Load Thai provincial minimum daily wage rates from a CSV.

    manage.py load_th_minimum_wage --file rates.csv [--dry-run]

Columns: province_code,province_name,daily_amount,effective_from
(ISO 3166-2:TH code such as TH-10; amount in baht; date as YYYY-MM-DD). An
optional ``note`` column is stored as given -- use it to say which announcement
the figures were taken from.

Nothing is shipped as law. ``payroll/data/th_minimum_wage_SAMPLE.csv`` shows
the format only; its figures are placeholders and must be replaced with the
Wage Committee's official announcement before the rates are relied on.

Idempotent per (province_code, effective_from): a row already loaded is
updated in place, so correcting a typo is a re-run rather than a cleanup. A
revision with a new effective date is a new row, which keeps past rates
checkable. The whole file is validated first and loaded in one transaction:
one bad row loads nothing.
"""

import csv
import datetime
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from payroll.models.minimum_wage import MinimumWageRate

REQUIRED = ("province_code", "province_name", "daily_amount", "effective_from")


def parse_rows(handle):
    """``[dict]`` ready for the model, or CommandError listing every bad line."""
    reader = csv.DictReader(handle)
    header = [name.strip() for name in (reader.fieldnames or [])]
    missing = [name for name in REQUIRED if name not in header]
    if missing:
        raise CommandError(f"Missing column(s): {', '.join(missing)}")
    reader.fieldnames = header

    rows, errors, seen = [], [], set()
    for line, raw in enumerate(reader, start=2):
        values = {key: (value or "").strip() for key, value in raw.items() if key}
        if not any(values.values()):
            continue
        try:
            amount = Decimal(values["daily_amount"].replace(",", ""))
            if amount <= 0:
                raise InvalidOperation
        except InvalidOperation:
            errors.append(f"line {line}: daily_amount must be a positive number")
            continue
        try:
            effective = datetime.date.fromisoformat(values["effective_from"])
        except ValueError:
            errors.append(f"line {line}: effective_from must be YYYY-MM-DD")
            continue
        row = MinimumWageRate(
            province_code=values["province_code"].upper(),
            province_name=values["province_name"],
            daily_amount=amount,
            effective_from=effective,
            note=values.get("note", "")[:255],
        )
        try:
            row.full_clean(validate_unique=False, validate_constraints=False)
        except ValidationError as exc:
            detail = "; ".join(
                f"{field}: {' '.join(messages)}"
                for field, messages in exc.message_dict.items()
            )
            errors.append(f"line {line}: {detail}")
            continue
        key = (row.province_code, row.effective_from)
        if key in seen:
            errors.append(
                f"line {line}: {row.province_code} from {row.effective_from} "
                "appears twice"
            )
            continue
        seen.add(key)
        rows.append(row)
    if errors:
        raise CommandError("Nothing loaded.\n" + "\n".join(errors))
    return rows


class Command(BaseCommand):
    help = (
        "Load provincial minimum daily wage rates from a CSV "
        "(province_code,province_name,daily_amount,effective_from)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", required=True, help="Path to the CSV file.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate and report without saving anything.",
        )

    def handle(self, *args, **options):
        try:
            with open(options["file"], newline="", encoding="utf-8-sig") as handle:
                rows = parse_rows(handle)
        except OSError as exc:
            raise CommandError(f"Cannot read {options['file']}: {exc}") from exc

        created = updated = 0
        with transaction.atomic():
            for row in rows:
                _obj, was_created = MinimumWageRate.objects.update_or_create(
                    province_code=row.province_code,
                    effective_from=row.effective_from,
                    defaults={
                        "province_name": row.province_name,
                        "daily_amount": row.daily_amount,
                        "note": row.note,
                        "is_active": True,
                    },
                )
                created += was_created
                updated += not was_created
            if options["dry_run"]:
                transaction.set_rollback(True)

        prefix = "Dry run: would have " if options["dry_run"] else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}created {created} and updated {updated} rate(s)."
            )
        )
        self.stdout.write(
            "Check every figure against the official minimum wage announcement; "
            "these rates are only as good as the file they came from."
        )
