"""
The provident fund and deduction-order sync that runs before a batch computes
payslips, and the daily scheduled sync.

A failed sync stops the run before anything is computed: payslips made with
stale benefit deductions would carry the wrong net pay.
"""

import logging
from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase

from payroll import scheduler
from payroll.methods import batch_run
from payroll.models.models import PayrollBatch, PayrollBatchLine
from payroll.tests.test_batch_run import END, EMPTY_LEAVES, START, Fixture, months

PVD = "orvahr_benefits.services.pvd.sync_pvd_deductions"
ORDERS = "orvahr_benefits.services.orders.sync_deduction_orders"


class BatchPreRunSyncTests(Fixture):
    def make_batch(self):
        return batch_run.create_batch(
            name="April", start_date=START, end_date=END, employees=self.people
        )

    def generate(self, batch, size=batch_run.SLICE_SIZE):
        with patch(
            "payroll.methods.methods.months_between_range", side_effect=months
        ), patch(
            "payroll.methods.methods.get_daily_salary",
            return_value={"day_wage": 1000.0},
        ), patch(
            "payroll.methods.methods.get_leaves", return_value=EMPTY_LEAVES
        ), patch(
            "attendance.methods.utils.get_employee_attendance_summary",
            return_value={},
        ):
            return batch_run.generate_slice(batch, size=size)

    def test_both_syncs_run_once_with_the_period_end_before_generation(self):
        batch = self.make_batch()
        with patch(PVD, return_value={}) as pvd, patch(ORDERS, return_value={}) as orders:
            while self.generate(batch, size=1):
                pass
        pvd.assert_called_once_with(END)
        orders.assert_called_once_with(END)
        batch.refresh_from_db()
        self.assertEqual(batch.progress_state, PayrollBatch.DONE)
        self.assertEqual(batch.payslips.count(), 3)

    def test_sync_runs_before_any_payslip(self):
        batch = self.make_batch()
        seen = []

        def record(on):
            seen.append(batch.payslips.count())
            return {}

        with patch(PVD, side_effect=record), patch(ORDERS, return_value={}):
            self.generate(batch)
        self.assertEqual(seen, [0])

    def test_failed_sync_stops_the_run_cleanly(self):
        batch = self.make_batch()
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        with patch(PVD, return_value={}), patch(ORDERS, side_effect=RuntimeError("db gone")):
            handled = self.generate(batch)

        self.assertEqual(handled, 0)
        self.assertEqual(batch.progress_state, PayrollBatch.FAILED)
        batch.refresh_from_db()
        self.assertEqual(batch.progress_state, PayrollBatch.FAILED)
        self.assertIn("deduction orders", batch.last_error)
        self.assertIn("db gone", batch.last_error)
        self.assertEqual(batch.payslips.count(), 0)
        self.assertEqual(batch.lines.filter(status=PayrollBatchLine.QUEUED).count(), 3)

        # A later slice request on the stopped run does nothing.
        with patch(PVD) as pvd, patch(ORDERS) as orders:
            self.assertEqual(self.generate(batch), 0)
        pvd.assert_not_called()
        orders.assert_not_called()
        self.assertEqual(batch.payslips.count(), 0)

    def test_a_failed_sync_rolls_back_its_own_writes(self):
        from payroll.models.models import Deduction

        def half_written(on):
            Deduction.objects.create(title="half written", amount=1)
            raise RuntimeError("boom")

        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        with patch(PVD, side_effect=half_written):
            with self.assertRaises(batch_run.BenefitSyncError):
                batch_run.sync_benefit_deductions(END)
        self.assertFalse(Deduction.objects.filter(title="half written").exists())

    def test_without_orvahr_benefits_nothing_is_synced(self):
        with patch("django.apps.apps.is_installed", return_value=False), patch(PVD) as pvd:
            self.assertEqual(batch_run.sync_benefit_deductions(END), {})
        pvd.assert_not_called()


class ScheduledSyncTests(TestCase):
    def test_daily_job_syncs_for_today(self):
        with patch(PVD, return_value={"members": 0}) as pvd, patch(
            ORDERS, return_value={}
        ) as orders:
            result = scheduler.sync_benefit_deductions_daily()
        pvd.assert_called_once_with(date.today())
        orders.assert_called_once_with(date.today())
        self.assertEqual(result["provident fund"], {"members": 0})

    def test_daily_job_swallows_a_failure(self):
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        with patch(PVD, side_effect=RuntimeError("boom")):
            self.assertIsNone(scheduler.sync_benefit_deductions_daily())

    def test_daily_job_is_registered(self):
        from horilla.scheduling import get_registered_jobs

        ids = {job.job_id for job in get_registered_jobs()}
        self.assertIn("sync_benefit_deductions_daily", ids)

    def test_automatic_generation_waits_for_a_failed_sync(self):
        from payroll.models.models import PayPeriodSettings

        setting = PayPeriodSettings.objects.create(auto_generate=True)
        _start, end = setting.last_completed_period(date(2026, 5, 1))
        today = end + timedelta(days=1)
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        with patch(PVD, side_effect=RuntimeError("boom")), patch.object(
            scheduler, "generate_for_setting"
        ) as generate:
            scheduler.generate_pay_period_payslips(today)
        generate.assert_not_called()
