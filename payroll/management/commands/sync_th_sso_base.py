"""
Set the Thai social security wage base on the SSO deduction from the engine's rules.

    manage.py sync_th_sso_base [--date YYYY-MM-DD] [--dry-run] [--rules-dir DIR]

The wage base (floor and ceiling the 5% is taken of) steps up by Ministerial
Regulation: 1,650-17,500 baht from 2026, 1,650-20,000 from 2029, 1,650-23,000
from 2032. The payroll engine ships one rules file per step
(``services/payroll-engine/rules/th-*.json``); this command reads the file in
force on ``--date`` (default today) -- the one with the latest
``effective_from`` on or before it -- and copies its ``social_security``
``base_min`` / ``base_max`` (satang) onto every SSO Deduction as
``contribution_base_min`` / ``contribution_base_max`` (baht).

The files are read directly; the engine service is not called. The directory
is ``--rules-dir``, else ``settings.ORVAHR_PAYROLL_ENGINE_RULES_DIR``, else the
copy in this repository.

Which deductions: the component library creates the SSO deduction with the
title of its ``th_sso`` entry and records nothing else that marks it, so rows
are matched on that title, case-insensitively, the same way the library itself
recognises an already-loaded component. A renamed SSO deduction is not found;
the command reports when it finds none.

Idempotent: rows already holding the figures are left untouched and counted
as unchanged. ``--dry-run`` reports what would change and writes nothing.
Run it after 1 Jan 2029 and 1 Jan 2032 (or schedule it), for each company.
"""

import datetime
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

SSO_LIBRARY_KEY = "th_sso"
SSO_FALLBACK_TITLE = "Social Security Fund (SSO)"
DEFAULT_RULES_DIR = (
    Path(__file__).resolve().parents[3] / "services" / "payroll-engine" / "rules"
)


def sso_title():
    """The title the component library gives the SSO deduction."""
    try:
        from payroll.component_library import COMPONENT_LIBRARY
    except ImportError:  # pragma: no cover - the library is part of payroll
        return SSO_FALLBACK_TITLE
    for entry in COMPONENT_LIBRARY:
        if entry.get("key") == SSO_LIBRARY_KEY:
            return entry["name"]
    return SSO_FALLBACK_TITLE


def rules_dir(option=None):
    return Path(
        option
        or getattr(settings, "ORVAHR_PAYROLL_ENGINE_RULES_DIR", None)
        or DEFAULT_RULES_DIR
    )


def sso_base_for(directory, on_date):
    """
    ``(base_min, base_max, file name, effective_from)`` in baht for the Thai
    rule set in force on ``on_date``, or CommandError.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise CommandError(f"Rules directory not found: {directory}")
    best = None
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("country") != "TH":
                continue
            effective = datetime.date.fromisoformat(data["effective_from"])
            ss = data["social_security"]
            base_min, base_max = int(ss["base_min"]), int(ss["base_max"])
        except (ValueError, KeyError, TypeError) as exc:
            raise CommandError(f"{path.name}: unreadable rules file ({exc})") from exc
        if effective > on_date:
            continue
        if best is not None and effective == best[3]:
            raise CommandError(
                f"{best[2]} and {path.name} share effective_from {effective}"
            )
        if best is None or effective > best[3]:
            best = (base_min, base_max, path.name, effective)
    if best is None:
        raise CommandError(f"No Thai rule set in {directory} is in force on {on_date}")
    base_min, base_max, name, effective = best
    if not 0 <= base_min <= base_max:
        raise CommandError(f"{name}: base_min must be between 0 and base_max")
    return base_min / 100, base_max / 100, name, effective


class Command(BaseCommand):
    help = (
        "Set contribution_base_min/max on the Thai SSO deduction(s) from the "
        "payroll engine rules file in force on a date."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            help="Date whose rule set applies, YYYY-MM-DD (default: today).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change without writing anything.",
        )
        parser.add_argument(
            "--rules-dir",
            help="Directory of engine rules files (default: the bundled ones).",
        )

    def handle(self, *args, **options):
        from payroll.models.models import Deduction

        if options.get("date"):
            try:
                on_date = datetime.date.fromisoformat(options["date"])
            except ValueError as exc:
                raise CommandError(f"--date must be YYYY-MM-DD: {exc}") from exc
        else:
            on_date = datetime.date.today()

        base_min, base_max, name, effective = sso_base_for(
            rules_dir(options.get("rules_dir")), on_date
        )
        title = sso_title()
        dry_run = options.get("dry_run", False)
        self.stdout.write(
            f"SSO wage base on {on_date}: {base_min:,.2f} - {base_max:,.2f} "
            f"(from {name}, effective {effective})"
        )

        rows = Deduction.objects.filter(title__iexact=title).order_by("pk")
        found = updated = unchanged = 0
        with transaction.atomic():
            for row in rows:
                found += 1
                if (
                    row.contribution_base_min == base_min
                    and row.contribution_base_max == base_max
                ):
                    unchanged += 1
                    continue
                updated += 1
                self.stdout.write(
                    f"  #{row.pk} {row.title}: "
                    f"{row.contribution_base_min} - {row.contribution_base_max}"
                    f" -> {base_min} - {base_max}"
                )
                if not dry_run:
                    row.contribution_base_min = base_min
                    row.contribution_base_max = base_max
                    row.save(
                        update_fields=[
                            "contribution_base_min",
                            "contribution_base_max",
                        ]
                    )

        if not found:
            self.stdout.write(
                self.style.WARNING(f'No deduction titled "{title}" was found.')
            )
        verb = "would update" if dry_run else "updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"{found} SSO deduction(s): {updated} {verb}, {unchanged} unchanged."
                + (" Dry run: nothing written." if dry_run else "")
            )
        )
