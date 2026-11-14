"""Tests for the load_thai_leave_types management command."""

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from horilla.testkit import make_company
from leave.management.commands.load_thai_leave_types import THAI_LEAVE_TYPES
from leave.models import LeaveType


class LoadThaiLeaveTypesTests(TestCase):
    def _run(self, *args):
        out, err = StringIO(), StringIO()
        call_command("load_thai_leave_types", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_names_fit_model(self):
        for spec in THAI_LEAVE_TYPES:
            self.assertLessEqual(len(spec["name"]), 30)

    def test_creates_and_is_idempotent(self):
        self._run()
        self.assertEqual(LeaveType.objects.count(), len(THAI_LEAVE_TYPES))
        self._run()
        self.assertEqual(LeaveType.objects.count(), len(THAI_LEAVE_TYPES))

    def test_existing_not_modified(self):
        LeaveType.objects.create(name="Sick Leave", total_days=99)
        self._run()
        self.assertEqual(LeaveType.objects.get(name="Sick Leave").total_days, 99)

    def test_dry_run_writes_nothing(self):
        out, err = self._run("--dry-run")
        self.assertEqual(LeaveType.objects.count(), 0)
        self.assertIn("would create", out)
        self.assertIn("unverified", err)

    def test_company_scoping(self):
        company = make_company("Leave Co")
        self._run("--company", str(company.pk))
        self.assertEqual(
            LeaveType.objects.filter(company_id=company).count(), len(THAI_LEAVE_TYPES)
        )
        self.assertFalse(LeaveType.objects.filter(company_id__isnull=True).exists())
        with self.assertRaises(CommandError):
            self._run("--company", "999999")
