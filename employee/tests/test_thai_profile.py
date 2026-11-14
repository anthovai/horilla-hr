"""EmployeeThaiProfile model tests. All IDs are synthetic."""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase

from employee.models import EmployeeThaiProfile
from employee.thai_validators import (
    is_valid_thai_national_id,
    mask_id,
    thai_national_id_checksum_digit,
    validate_thai_national_id,
)
from horilla.testkit import make_company, make_employee


def synthetic_id(first_twelve):
    """Build a valid 13-digit ID from 12 digits using the mod-11 rule."""
    return first_twelve + str(thai_national_id_checksum_digit(first_twelve))


class ChecksumTests(SimpleTestCase):
    def test_hand_computed_value(self):
        # digits 1..9,0,1,2 weighted 13..2: 13+24+33+40+45+48+49+48+45+0+3+4
        # = 352; 352 % 11 = 0; (11-0) % 10 = 1 -> valid ID is 1234567890121
        self.assertEqual(thai_national_id_checksum_digit("123456789012"), 1)
        self.assertTrue(is_valid_thai_national_id("1234567890121"))

    def test_wrong_check_digit_invalid(self):
        self.assertFalse(is_valid_thai_national_id("1234567890123"))
        with self.assertRaises(ValidationError):
            validate_thai_national_id("1234567890123")

    def test_every_check_digit_but_one_rejected(self):
        base = "310000000001"
        good = synthetic_id(base)
        bad = [base + str(d) for d in range(10) if str(d) != good[-1]]
        self.assertTrue(is_valid_thai_national_id(good))
        self.assertFalse(any(is_valid_thai_national_id(b) for b in bad))

    def test_remainder_edge_cases_map_to_digit(self):
        # remainder 0 -> 11 % 10 = 1 ; remainder 1 -> 10 % 10 = 0
        for first in ("100000000000", "200000000000", "300000000000"):
            d = thai_national_id_checksum_digit(first)
            self.assertIn(d, range(10))
            self.assertTrue(is_valid_thai_national_id(first + str(d)))

    def test_bad_format(self):
        for v in ("", None, "123", "12345678901234", "12345678901ab", "abcdefghijklm"):
            self.assertFalse(is_valid_thai_national_id(v))

    def test_separators_tolerated(self):
        self.assertTrue(is_valid_thai_national_id("1-2345-67890-12-1"))

    def test_mask(self):
        self.assertEqual(mask_id("1234567890121"), "XXXXXXXXX0121")
        self.assertEqual(mask_id(None), "")
        self.assertEqual(mask_id("12"), "12")


class ThaiProfileModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        company = make_company("Thai Co")
        cls.e1 = make_employee(company=company, email="t1@thai.test", phone="9100000001")
        cls.e2 = make_employee(company=company, email="t2@thai.test", phone="9100000002")
        cls.nid1 = synthetic_id("310000000001")
        cls.nid2 = synthetic_id("310000000002")

    def test_tax_id_defaults_to_national_id(self):
        p = EmployeeThaiProfile.objects.create(employee_id=self.e1, national_id=self.nid1)
        self.assertEqual(p.tax_id, self.nid1)

    def test_explicit_tax_id_kept(self):
        p = EmployeeThaiProfile.objects.create(
            employee_id=self.e1, national_id=self.nid1, tax_id=self.nid2
        )
        self.assertEqual(p.tax_id, self.nid2)

    def test_full_clean_rejects_bad_checksum(self):
        p = EmployeeThaiProfile(employee_id=self.e1, national_id="1234567890123")
        with self.assertRaises(ValidationError) as ctx:
            p.full_clean()
        self.assertIn("national_id", ctx.exception.message_dict)

    def test_national_id_unique_when_set(self):
        EmployeeThaiProfile.objects.create(employee_id=self.e1, national_id=self.nid1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            EmployeeThaiProfile.objects.create(
                employee_id=self.e2, national_id=self.nid1
            )

    def test_unique_validation_in_full_clean(self):
        EmployeeThaiProfile.objects.create(employee_id=self.e1, national_id=self.nid1)
        p = EmployeeThaiProfile(employee_id=self.e2, national_id=self.nid1)
        with self.assertRaises(ValidationError) as ctx:
            p.full_clean()
        self.assertIn("national_id", ctx.exception.message_dict)

    def test_blank_national_id_not_unique_constrained(self):
        EmployeeThaiProfile.objects.create(employee_id=self.e1, national_id="")
        p2 = EmployeeThaiProfile.objects.create(employee_id=self.e2, national_id="")
        self.assertIsNone(p2.national_id)

    def test_str_and_repr_are_masked(self):
        p = EmployeeThaiProfile.objects.create(employee_id=self.e1, national_id=self.nid1)
        for text in (str(p), repr(p)):
            self.assertNotIn(self.nid1, text)
            self.assertIn(self.nid1[-4:], text)

    def test_postcode_and_promptpay_validation(self):
        p = EmployeeThaiProfile(employee_id=self.e1, postcode="1050", promptpay_id="123")
        with self.assertRaises(ValidationError) as ctx:
            p.full_clean()
        self.assertIn("postcode", ctx.exception.message_dict)
        self.assertIn("promptpay_id", ctx.exception.message_dict)

    def test_bank_code_choices(self):
        codes = {c for c, _ in EmployeeThaiProfile.BANK_CHOICES}
        self.assertEqual(
            codes,
            {"002", "004", "006", "011", "014", "022", "024", "025", "030", "034"},
        )

    def test_sensitive_fields_not_in_admin_list_or_search(self):
        from django.contrib import admin

        ma = admin.site._registry[EmployeeThaiProfile]
        exposed = set(ma.list_display) | set(ma.search_fields) | set(ma.list_filter)
        for f in EmployeeThaiProfile.SENSITIVE_FIELDS:
            self.assertFalse(any(f in e for e in exposed))
