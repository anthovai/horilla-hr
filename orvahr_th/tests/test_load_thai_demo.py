"""load_thai_demo: synthetic IDs are checksum-valid, re-runs and dry runs write nothing."""

import datetime
import io
import re

from django.core.management import call_command
from django.test import TestCase

from employee.models import EmployeeBankDetails, EmployeeTaxClaim, EmployeeThaiProfile
from employee.thai_validators import is_valid_thai_national_id
from horilla.testkit import make_company, make_employee
from horilla.testkit.factories import make_contract
from orvahr_th.management.commands import load_thai_demo as demo


def run(*args):
    out = io.StringIO()
    call_command("load_thai_demo", *args, stdout=out, stderr=io.StringIO())
    return out.getvalue()


def snapshot():
    from base.models import Holidays
    from leave.models import LeaveType
    from orvahr_bank.models import CompanyBankAccount
    from orvahr_benefits.models import (
        DeductionOrder,
        EmployeeProvidentFund,
        PVDVestingStep,
        ProvidentFundPlan,
    )
    from orvahr_filing.models import CompanyTaxProfile
    from orvahr_pdpa.models import ConsentPurpose, PdpaSettings, PrivacyNotice
    from payroll.models.models import Contract, Deduction, FilingStatus
    from payroll.models.tax_models import PayrollSettings

    counts = {
        model.__name__: model._base_manager.count()
        for model in (
            Holidays, LeaveType, CompanyBankAccount, DeductionOrder,
            EmployeeProvidentFund, PVDVestingStep, ProvidentFundPlan,
            CompanyTaxProfile, ConsentPurpose, PdpaSettings, PrivacyNotice,
            Deduction, FilingStatus, PayrollSettings, EmployeeThaiProfile,
            EmployeeTaxClaim, EmployeeBankDetails,
        )
    }
    counts["deduction_members"] = Deduction.specific_employees.through.objects.count()
    counts["currencies"] = sorted(
        PayrollSettings._base_manager.values_list("currency_symbol", flat=True)
    )
    counts["contract_status"] = sorted(
        Contract._base_manager.values_list("filing_status_id", flat=True),
        key=lambda v: v or 0,
    )
    counts["bank"] = sorted(
        EmployeeBankDetails._base_manager.values_list("bank_name", "account_number")
    )
    return counts


class LoadThaiDemoTests(TestCase):
    def setUp(self):
        from payroll.models.models import Contract

        self.company = make_company("Demo One")
        self.other = make_company("Demo Two")
        self.employees = []
        for i in range(8):
            company = self.company if i < 6 else self.other
            employee = make_employee(
                company=company,
                email=f"demo{i}@test.horilla",
                first_name=f"Demo{i}",
                date_joining=datetime.date(2021, 1, 1),
            )
            Contract._base_manager.filter(employee_id=employee).delete()
            contract = make_contract(
                employee=employee, start_date=datetime.date(2021, 1, 1), wage=30000 + i * 1000
            )
            Contract._base_manager.filter(pk=contract.pk).update(contract_status="active")
            self.employees.append(employee)
        EmployeeBankDetails.objects.create(
            employee_id=self.employees[0], bank_name="Chase", account_number="ACC-1"
        )

    def test_check_digit_helpers(self):
        for serial in (0, 1, 7, 288, 9_999_999):
            value = demo.synthetic_national_id(serial)
            self.assertEqual(len(value), 13)
            self.assertTrue(is_valid_thai_national_id(value))
            self.assertTrue(value.startswith("19999"))
        for serial in (1, 2, 3, 99_999):
            value = demo.synthetic_company_tax_id(serial)
            self.assertTrue(is_valid_thai_national_id(value))
            self.assertTrue(value.startswith("0105599"))
        # Changing any one digit breaks the check digit.
        value = demo.synthetic_national_id(288)
        self.assertFalse(is_valid_thai_national_id(value[:12] + str((int(value[12]) + 1) % 10)))

    def test_generated_ids_are_valid_and_unique(self):
        from orvahr_filing.models import CompanyTaxProfile

        output = run()
        profiles = list(EmployeeThaiProfile._base_manager.all())
        self.assertEqual(len(profiles), len(self.employees))
        ids = [p.national_id for p in profiles]
        self.assertEqual(len(set(ids)), len(ids))
        for profile in profiles:
            self.assertTrue(is_valid_thai_national_id(profile.national_id))
            self.assertEqual(profile.tax_id, profile.national_id)
            self.assertTrue(profile.first_name_th and profile.last_name_th)
            self.assertNotIn(profile.national_id, output)
        tax_profiles = list(CompanyTaxProfile._base_manager.all())
        self.assertEqual(len(tax_profiles), 2)
        for profile in tax_profiles:
            self.assertTrue(is_valid_thai_national_id(profile.tax_id))
            self.assertNotIn(profile.tax_id, output)
        # Bank details now hold a 10-15 digit account at a Thai bank.
        bank = EmployeeBankDetails._base_manager.get(employee_id=self.employees[0])
        self.assertTrue(bank.account_number.isdigit())
        self.assertNotIn(bank.account_number, output)

    def test_wires_payroll(self):
        from orvahr_pdpa.models import PrivacyNotice
        from payroll.models.models import Contract, Deduction
        from payroll.models.tax_models import PayrollSettings

        run()
        self.assertEqual(
            set(PayrollSettings._base_manager.values_list("currency_symbol", flat=True)), {"฿"}
        )
        statuses = set(
            Contract._base_manager.filter(contract_status="active").values_list(
                "filing_status__filing_status", flat=True
            )
        )
        self.assertEqual(len(statuses), 1)
        self.assertIn("Thailand", statuses.pop())
        sso = Deduction._base_manager.get(title="Social Security Fund (SSO)")
        registered = EmployeeThaiProfile._base_manager.filter(
            social_security_no__isnull=False
        ).count()
        self.assertEqual(sso.specific_employees.count(), registered)
        self.assertTrue(
            PrivacyNotice.current("employee", company=self.company).text_th.startswith(
                "[" + demo.DEMO_TAG
            )
        )

    def test_idempotent(self):
        run()
        first = snapshot()
        output = run()
        self.assertEqual(snapshot(), first)
        self.assertIsNone(re.search(r"(created|enrolled|given|set to)[^:]*: [1-9]", output))

    def test_dry_run_writes_nothing(self):
        before = snapshot()
        output = run("--dry-run")
        self.assertIn("dry-run", output)
        self.assertIn("Employee Thai profiles created: 8", output)
        self.assertEqual(snapshot(), before)

    def test_single_company(self):
        run("--company", str(self.other.pk))
        self.assertEqual(EmployeeThaiProfile._base_manager.count(), 2)


class ForeignStatutoryDeductionTests(TestCase):
    def test_india_statutory_deductions_are_switched_off_not_deleted(self):
        from payroll.models.models import Deduction

        make_company("Foreign Co")
        pf = Deduction.objects.create(title="Provident Fund (PF)", is_fixed=True, amount=100)
        other = Deduction.objects.create(title="Canteen", is_fixed=True, amount=50)
        run()
        pf.refresh_from_db()
        other.refresh_from_db()
        self.assertFalse(pf.is_active)
        self.assertTrue(other.is_active)
