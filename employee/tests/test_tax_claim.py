"""EmployeeTaxClaim model tests."""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from employee.models import EmployeeTaxClaim
from employee.tax_claims import CAPPED_CODES, CLAIM_CODE_CHOICES, PER_UNIT_CODES
from horilla.testkit import make_company, make_employee


class TaxClaimTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        company = make_company("Claim Co")
        cls.e1 = make_employee(company=company, email="c1@claim.test", phone="9200000001")
        cls.e2 = make_employee(company=company, email="c2@claim.test", phone="9200000002")

    def test_codes_are_partitioned_and_all_offered(self):
        offered = {c for c, _ in CLAIM_CODE_CHOICES}
        self.assertEqual(offered, set(PER_UNIT_CODES) | set(CAPPED_CODES))
        self.assertFalse(set(PER_UNIT_CODES) & set(CAPPED_CODES))

    def test_per_unit_claim_saves_with_units(self):
        c = EmployeeTaxClaim.objects.create(employee_id=self.e1, tax_year=2026, code="child", units=2)
        self.assertEqual(self.e1.tax_claims.get().pk, c.pk)

    def test_capped_claim_saves_with_amount(self):
        EmployeeTaxClaim.objects.create(employee_id=self.e1, tax_year=2026, code="rmf", amount=40000)
        self.assertEqual(self.e1.tax_claims.get().amount, 40000)

    def test_wrong_shape_is_rejected(self):
        bad = [
            dict(code="child"),  # no units
            dict(code="child", units=0),
            dict(code="child", units=1, amount=10),
            dict(code="rmf"),  # no amount
            dict(code="rmf", amount=0),
            dict(code="rmf", amount=10, units=1),
        ]
        for kw in bad:
            with self.subTest(kw=kw), self.assertRaises(ValidationError):
                EmployeeTaxClaim.objects.create(employee_id=self.e1, tax_year=2026, **kw)

    def test_unknown_code_is_rejected(self):
        with self.assertRaises(ValidationError):
            EmployeeTaxClaim.objects.create(employee_id=self.e1, tax_year=2026, code="yacht", amount=1)

    def test_one_claim_per_employee_year_code(self):
        EmployeeTaxClaim.objects.create(employee_id=self.e1, tax_year=2026, code="child", units=1)
        with self.assertRaises((IntegrityError, ValidationError)), transaction.atomic():
            EmployeeTaxClaim.objects.create(employee_id=self.e1, tax_year=2026, code="child", units=2)
        # other year / other employee are fine
        EmployeeTaxClaim.objects.create(employee_id=self.e1, tax_year=2027, code="child", units=2)
        EmployeeTaxClaim.objects.create(employee_id=self.e2, tax_year=2026, code="child", units=2)
