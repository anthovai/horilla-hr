"""
Load Thai public holidays for one year into ``Holidays``.

IMPORTANT - everything here is a STARTING POINT, not an authority:

* Fixed-date holidays come from ``base/data/th_fixed_holidays.json``. The Thai
  Cabinet announces the official list every year (and may add special
  holidays or move days), so verify against the current announcement.
* Lunar / variable holidays (Makha Bucha, Visakha Bucha, Asarnha Bucha,
  Khao Phansa, ...) and substitution days change every year. This command
  never guesses them; supply them with ``--file`` (CSV columns:
  ``date,name[,department]``, ISO dates) copied from the official
  announcement.
* ``--substitute-weekends`` is an opt-in convention (next non-holiday
  weekday for a fixed holiday on Sat/Sun). Confirm it matches the official
  announcement and your company policy before using it.

Holidays are matched on name + start_date + company, so re-running is
idempotent. ``Holidays`` allows only one holiday per start/end date and
company; a different-named holiday already on the date is reported as a
conflict and skipped.
"""

import csv
import json
from datetime import date, timedelta
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from base.models import Company, Department, Holidays

FIXED_FILE = Path(__file__).resolve().parents[2] / "data" / "th_fixed_holidays.json"
NAME_MAX = 30  # Holidays.name max_length


def load_fixed_data():
    with open(FIXED_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def substitute_name(name):
    return f"Substitute {name}"[:NAME_MAX]


class Command(BaseCommand):
    help = (
        "Load Thai public holidays for a year (fixed list + optional CSV of "
        "variable days). Idempotent. Verify against the official announcement."
    )

    def add_arguments(self, parser):
        parser.add_argument("--year", type=int, required=True)
        parser.add_argument(
            "--file",
            help="CSV (date,name[,department]) of variable/substitution holidays "
            "from the official announcement.",
        )
        parser.add_argument("--company", type=int, help="Company ID (default: global)")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--substitute-weekends",
            action="store_true",
            help="Opt-in, UNVERIFIED convention: add a substitute day (next "
            "free weekday) for fixed holidays falling on Sat/Sun.",
        )

    # ------------------------------------------------------------------ parse
    def _read_csv(self, path, year):
        """Return list of dicts; raises CommandError listing every bad row."""
        errors, rows, seen = [], [], {}
        try:
            fh = open(path, newline="", encoding="utf-8-sig")
        except OSError as exc:
            raise CommandError(f"Cannot read file {path}: {exc}")
        with fh:
            reader = csv.DictReader(fh)
            fields = [(f or "").strip().lower() for f in (reader.fieldnames or [])]
            if "date" not in fields or "name" not in fields:
                raise CommandError("CSV must have header columns: date,name[,department]")
            reader.fieldnames = fields
            for lineno, rec in enumerate(reader, start=2):
                raw_date = (rec.get("date") or "").strip()
                name = (rec.get("name") or "").strip()
                dept = (rec.get("department") or "").strip()
                if not raw_date and not name:
                    continue
                try:
                    day = date.fromisoformat(raw_date)
                except ValueError:
                    errors.append(f"line {lineno}: invalid date {raw_date!r} (use YYYY-MM-DD)")
                    continue
                if day.year != year:
                    errors.append(f"line {lineno}: date {day} is not in year {year}")
                    continue
                if not name:
                    errors.append(f"line {lineno}: missing name")
                    continue
                if len(name) > NAME_MAX:
                    errors.append(f"line {lineno}: name longer than {NAME_MAX} characters")
                    continue
                if day in seen:
                    errors.append(
                        f"line {lineno}: duplicate date {day} (first on line {seen[day]}); "
                        "only one holiday per date and company is allowed"
                    )
                    continue
                seen[day] = lineno
                department = None
                if dept:
                    matches = list(Department.objects.filter(department__iexact=dept))
                    if len(matches) != 1:
                        errors.append(
                            f"line {lineno}: department {dept!r} matched {len(matches)} records"
                        )
                        continue
                    department = matches[0]
                rows.append(
                    {"date": day, "name": name, "department": department, "source": "file"}
                )
        if errors:
            raise CommandError("CSV errors, nothing written:\n  " + "\n  ".join(errors))
        return rows

    # ----------------------------------------------------------------- handle
    def handle(self, *args, **opts):
        year = opts["year"]
        if not 1900 <= year <= 2200:
            raise CommandError("--year out of range")
        company = None
        if opts["company"] is not None:
            try:
                company = Company.objects.get(pk=opts["company"])
            except Company.DoesNotExist:
                raise CommandError(f"Company {opts['company']} not found")
        dry = opts["dry_run"]

        data = load_fixed_data()
        file_rows = self._read_csv(opts["file"], year) if opts["file"] else []
        file_dates = {r["date"] for r in file_rows}

        planned = []
        for item in data["holidays"]:
            day = date(year, item["month"], item["day"])
            if day in file_dates:
                self.stdout.write(
                    f"Note: {day} supplied in --file; fixed entry {item['name']!r} skipped."
                )
                continue
            planned.append(
                {"date": day, "name": item["name"], "department": None, "source": "fixed"}
            )
        planned_fixed = list(planned)
        planned.extend(file_rows)

        if opts["substitute_weekends"]:
            taken = {p["date"] for p in planned}
            for p in planned_fixed:
                if p["date"].weekday() < 5:
                    continue
                sub = p["date"] + timedelta(days=1)
                while sub.weekday() >= 5 or sub in taken:
                    sub += timedelta(days=1)
                if sub.year != year:
                    self.stdout.write(f"Note: substitute for {p['name']} falls outside {year}; skipped.")
                    continue
                taken.add(sub)
                planned.append(
                    {"date": sub, "name": substitute_name(p["name"]), "department": None,
                     "source": "substitute"}
                )

        # Warn about variable holidays that were not supplied.
        names = " ".join(r["name"].lower() for r in file_rows)
        missing = [
            v["label"]
            for v in data["variable_holidays_expected"]
            if not any(m in names for m in v["match"])
        ]
        if missing:
            self.stderr.write(
                self.style.WARNING(
                    "WARNING: variable (lunar) holidays NOT loaded: "
                    + ", ".join(missing)
                    + ". Their dates change yearly; supply them with --file from the "
                    "official Cabinet announcement (also any substitution days or "
                    "special holidays). Fixed dates here are also unverified."
                )
            )

        created = existing = conflicts = 0
        with transaction.atomic():
            for p in sorted(planned, key=lambda x: x["date"]):
                qs = Holidays.objects.filter(start_date=p["date"], company_id=company)
                if qs.filter(name=p["name"]).exists():
                    existing += 1
                    self.stdout.write(f"exists    {p['date']} {p['name']}")
                    continue
                other = qs.first()
                if other:
                    conflicts += 1
                    self.stderr.write(
                        self.style.WARNING(
                            f"conflict  {p['date']} {p['name']} (existing: {other.name!r}); skipped"
                        )
                    )
                    continue
                created += 1
                self.stdout.write(
                    f"{'would create' if dry else 'create'} {p['date']} {p['name']} [{p['source']}]"
                )
                if dry:
                    continue
                holiday = Holidays.objects.create(
                    name=p["name"],
                    start_date=p["date"],
                    end_date=p["date"],
                    company_id=company,
                    is_specific=bool(p["department"]),
                    assigning_type="department" if p["department"] else None,
                )
                if p["department"]:
                    holiday.department.add(p["department"])
        self.stdout.write(
            self.style.SUCCESS(
                f"{'[dry-run] ' if dry else ''}{created} to create, {existing} existing, "
                f"{conflicts} conflicts."
            )
        )
