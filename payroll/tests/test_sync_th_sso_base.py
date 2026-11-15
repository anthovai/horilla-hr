"""
sync_th_sso_base: the Thai SSO wage base follows the engine's rules files.

The base steps up on 1 Jan 2029 and 1 Jan 2032. The engine ships a rules file
per step; this command copies the step in force onto the SSO deduction so the
Django calculation and the engine agree without anyone retyping figures.
"""

import datetime
import json
import shutil
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from payroll.component_library import load_components
from payroll.management.commands.sync_th_sso_base import DEFAULT_RULES_DIR, sso_base_for
from payroll.models.models import Deduction


def _rules(effective_from, base_max, base_min=165000, country="TH"):
    return {
        "country": country,
        "year": int(effective_from[:4]),
        "effective_from": effective_from,
        "social_security": {"rate_bp": 500, "base_min": base_min, "base_max": base_max},
    }


class TempRulesDir:
    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.write("a.json", _rules("2026-01-01", 1750000))
        self.write("b.json", _rules("2029-01-01", 2000000))
        self.write("c.json", _rules("2032-01-01", 2300000))
        (self.dir / "notes.txt").write_text("not a rules file")

    def write(self, name, data):
        (self.dir / name).write_text(json.dumps(data), encoding="utf-8")


class SsoBaseLookupTests(TempRulesDir, SimpleTestCase):
    def test_picks_the_latest_set_in_force(self):
        for day, expected in [
            ("2026-01-01", 17500.0),
            ("2028-12-31", 17500.0),
            ("2029-01-01", 20000.0),
            ("2031-12-31", 20000.0),
            ("2032-06-01", 23000.0),
        ]:
            base_min, base_max, _name, _eff = sso_base_for(
                self.dir, datetime.date.fromisoformat(day)
            )
            self.assertEqual((base_min, base_max), (1650.0, expected), day)

    def test_before_any_set_is_an_error(self):
        with self.assertRaises(CommandError):
            sso_base_for(self.dir, datetime.date(2025, 12, 31))

    def test_other_countries_are_ignored(self):
        self.write("x.json", _rules("2030-01-01", 1, country="XX"))
        _min, base_max, _n, _e = sso_base_for(self.dir, datetime.date(2030, 6, 1))
        self.assertEqual(base_max, 20000.0)

    def test_duplicate_effective_dates_are_an_error(self):
        self.write("d.json", _rules("2029-01-01", 2100000))
        with self.assertRaises(CommandError):
            sso_base_for(self.dir, datetime.date(2030, 1, 1))

    def test_the_shipped_engine_rules_carry_the_gazetted_steps(self):
        for day, expected in [
            ("2028-12-31", 17500.0),
            ("2029-01-01", 20000.0),
            ("2032-01-01", 23000.0),
        ]:
            base_min, base_max, _n, _e = sso_base_for(
                DEFAULT_RULES_DIR, datetime.date.fromisoformat(day)
            )
            self.assertEqual((base_min, base_max), (1650.0, expected), day)


class SyncCommandTests(TempRulesDir, TestCase):
    def setUp(self):
        super().setUp()
        [self.sso] = load_components(["th_sso"])
        self.other = Deduction.objects.create(
            title="Provident Fund",
            is_fixed=False,
            based_on="gross_pay",
            rate=5.0,
            contribution_base_min=1.0,
            contribution_base_max=2.0,
        )

    def run_cmd(self, *args, **kwargs):
        out = StringIO()
        call_command("sync_th_sso_base", *args, stdout=out, **kwargs)
        return out.getvalue()

    def _bases(self, row):
        row.refresh_from_db()
        return (row.contribution_base_min, row.contribution_base_max)

    def test_updates_the_sso_deduction_only_and_is_idempotent(self):
        with self.settings(ORVAHR_PAYROLL_ENGINE_RULES_DIR=str(self.dir)):
            out = self.run_cmd("--date", "2029-01-01")
            self.assertIn("1 updated", out)
            self.assertEqual(self._bases(self.sso), (1650.0, 20000.0))
            self.assertEqual(self._bases(self.other), (1.0, 2.0))

            out = self.run_cmd("--date", "2029-06-01")
            self.assertIn("0 updated, 1 unchanged", out)

    def test_rules_dir_argument_overrides_the_setting(self):
        with self.settings(ORVAHR_PAYROLL_ENGINE_RULES_DIR="/nonexistent"):
            self.run_cmd("--date", "2032-06-01", "--rules-dir", str(self.dir))
        self.assertEqual(self._bases(self.sso), (1650.0, 23000.0))

    def test_dry_run_writes_nothing(self):
        out = self.run_cmd("--date", "2032-01-01", "--dry-run", rules_dir=str(self.dir))
        self.assertIn("1 would update", out)
        self.assertIn("Dry run", out)
        self.assertEqual(self._bases(self.sso), (1650.0, 17500.0))

    def test_matches_the_title_case_insensitively(self):
        Deduction.objects.filter(pk=self.sso.pk).update(
            title="social security fund (sso)"
        )
        self.run_cmd("--date", "2029-01-01", rules_dir=str(self.dir))
        self.assertEqual(self._bases(self.sso), (1650.0, 20000.0))

    def test_reports_when_no_sso_deduction_exists(self):
        Deduction.objects.filter(pk=self.sso.pk).delete()
        out = self.run_cmd("--date", "2029-01-01", rules_dir=str(self.dir))
        self.assertIn("No deduction titled", out)
        self.assertIn("0 SSO deduction(s)", out)

    def test_bad_date_is_an_error(self):
        with self.assertRaises(CommandError):
            self.run_cmd("--date", "01/01/2029", rules_dir=str(self.dir))
