"""Tests for the load_thai_holidays management command."""

import os
import tempfile
from datetime import date
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from base.models import Department, Holidays
from horilla.testkit import make_company


class LoadThaiHolidaysTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company("Thai Co")
        cls.other = make_company("Other Co")

    def _csv(self, text):
        fh = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
        fh.write(text)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def _run(self, *args):
        out, err = StringIO(), StringIO()
        call_command("load_thai_holidays", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_idempotent(self):
        self._run("--year", "2027")
        n = Holidays.objects.count()
        self.assertGreater(n, 10)
        self._run("--year", "2027")
        self.assertEqual(Holidays.objects.count(), n)

    def test_dry_run_writes_nothing(self):
        out, _ = self._run("--year", "2027", "--dry-run")
        self.assertEqual(Holidays.objects.count(), 0)
        self.assertIn("would create", out)

    def test_warns_missing_variable_holidays(self):
        _, err = self._run("--year", "2027", "--dry-run")
        for label in ("Makha Bucha", "Visakha Bucha", "Asarnha Bucha", "Khao Phansa"):
            self.assertIn(label, err)

    def test_file_satisfies_warning_and_loads(self):
        path = self._csv(
            "date,name\n2027-02-20,Makha Bucha Day\n2027-05-20,Visakha Bucha Day\n"
            "2027-07-18,Asarnha Bucha Day\n2027-07-19,Khao Phansa Day\n"
        )
        _, err = self._run("--year", "2027", "--file", path)
        self.assertNotIn("NOT loaded", err)
        self.assertTrue(Holidays.objects.filter(name="Makha Bucha Day").exists())

    def test_csv_errors_write_nothing(self):
        path = self._csv("date,name\nnot-a-date,X\n2026-01-01,Wrong Year\n2027-03-01,\n")
        with self.assertRaises(CommandError) as ctx:
            self._run("--year", "2027", "--file", path)
        msg = str(ctx.exception)
        self.assertIn("invalid date", msg)
        self.assertIn("not in year", msg)
        self.assertIn("missing name", msg)
        self.assertEqual(Holidays.objects.count(), 0)

    def test_csv_bad_header_and_unknown_department(self):
        with self.assertRaises(CommandError):
            self._run("--year", "2027", "--file", self._csv("when,what\n2027-01-01,X\n"))
        with self.assertRaises(CommandError):
            self._run(
                "--year", "2027", "--file",
                self._csv("date,name,department\n2027-03-01,Foo,Nope\n"),
            )

    def test_csv_department_scoping(self):
        dept = Department.objects.create(department="Ops")
        path = self._csv("date,name,department\n2027-03-01,Dept Day,Ops\n")
        self._run("--year", "2027", "--file", path)
        h = Holidays.objects.get(name="Dept Day")
        self.assertTrue(h.is_specific)
        self.assertEqual(list(h.department.all()), [dept])

    def test_company_scoping(self):
        self._run("--year", "2027", "--company", str(self.company.pk))
        self.assertFalse(Holidays.objects.filter(company_id__isnull=True).exists())
        self.assertFalse(Holidays.objects.filter(company_id=self.other).exists())
        # Same dates for another company are independent.
        self._run("--year", "2027", "--company", str(self.other.pk))
        self.assertEqual(
            Holidays.objects.filter(company_id=self.company).count(),
            Holidays.objects.filter(company_id=self.other).count(),
        )

    def test_unknown_company(self):
        with self.assertRaises(CommandError):
            self._run("--year", "2027", "--company", "999999")

    def test_conflicting_name_on_date_skipped(self):
        Holidays.objects.create(
            name="Custom", start_date=date(2027, 1, 1), end_date=date(2027, 1, 1)
        )
        _, err = self._run("--year", "2027")
        self.assertIn("conflict", err)
        self.assertFalse(Holidays.objects.filter(name="New Year's Day").exists())

    def test_substitute_weekends_opt_in(self):
        # 2027-05-01 is a Saturday; 2027-04-06 Tuesday.
        self._run("--year", "2027")
        self.assertFalse(Holidays.objects.filter(name__startswith="Substitute").exists())
        self._run("--year", "2027", "--substitute-weekends")
        sub = Holidays.objects.get(name="Substitute National Labour Day")
        self.assertEqual(sub.start_date.weekday(), 0)  # Monday
