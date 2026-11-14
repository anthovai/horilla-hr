"""
Pure formatting helpers. The baht_text cases mirror web/src/lib/baht-text.test.ts
(which takes satang integers; these take baht), plus the Python-only rules:
Decimal half-up rounding, float/str input and rejection of junk.
"""

import datetime
from decimal import Decimal

from django.test import SimpleTestCase

from orvahr_th import formatting as f

# (satang as in baht-text.test.ts, expected)
TS_CASES = [
    (0, "ศูนย์บาทถ้วน"),
    (1, "ศูนย์บาทหนึ่งสตางค์"),
    (100, "หนึ่งบาทถ้วน"),
    (1100, "สิบเอ็ดบาทถ้วน"),
    (2100, "ยี่สิบเอ็ดบาทถ้วน"),
    (2000, "ยี่สิบบาทถ้วน"),
    (1000, "สิบบาทถ้วน"),
    (10100, "หนึ่งร้อยเอ็ดบาทถ้วน"),
    (123450, "หนึ่งพันสองร้อยสามสิบสี่บาทห้าสิบสตางค์"),
    (100_000_100, "หนึ่งล้านเอ็ดบาทถ้วน"),
    (100_000_000, "หนึ่งล้านบาทถ้วน"),
    (1_000_000_000, "สิบล้านบาทถ้วน"),
    (2125, "ยี่สิบเอ็ดบาทยี่สิบห้าสตางค์"),
    (101, "หนึ่งบาทหนึ่งสตางค์"),
    (121, "หนึ่งบาทยี่สิบเอ็ดสตางค์"),
    (500_000, "ห้าพันบาทถ้วน"),
    (123_456_789, "หนึ่งล้านสองแสนสามหมื่นสี่พันห้าร้อยหกสิบเจ็ดบาทแปดสิบเก้าสตางค์"),
    (1_000_000_000_000, "หนึ่งหมื่นล้านบาทถ้วน"),
    (-123450, "ลบหนึ่งพันสองร้อยสามสิบสี่บาทห้าสิบสตางค์"),
    (100_000_000_000_000, "หนึ่งล้านล้านบาทถ้วน"),
]


class BahtTextTests(SimpleTestCase):
    def test_mirrors_typescript_cases(self):
        for satang, expected in TS_CASES:
            with self.subTest(satang=satang):
                baht = Decimal(satang) / 100
                self.assertEqual(f.baht_text(baht), expected)

    def test_task_examples(self):
        self.assertEqual(f.baht_text(0), "ศูนย์บาทถ้วน")
        self.assertEqual(f.baht_text("0.01"), "ศูนย์บาทหนึ่งสตางค์")
        self.assertEqual(f.baht_text(1), "หนึ่งบาทถ้วน")
        self.assertEqual(f.baht_text(11), "สิบเอ็ดบาทถ้วน")
        self.assertEqual(f.baht_text(21), "ยี่สิบเอ็ดบาทถ้วน")
        self.assertEqual(f.baht_text(101), "หนึ่งร้อยเอ็ดบาทถ้วน")
        self.assertEqual(f.baht_text(1_000_000), "หนึ่งล้านบาทถ้วน")
        self.assertEqual(f.baht_text(1_000_001), "หนึ่งล้านเอ็ดบาทถ้วน")
        self.assertEqual(f.baht_text(10_000_000), "สิบล้านบาทถ้วน")
        self.assertEqual(f.baht_text(10**12), "หนึ่งล้านล้านบาทถ้วน")
        self.assertEqual(f.baht_text(2 * 10**12 + 1), "สองล้านล้านเอ็ดบาทถ้วน")
        self.assertEqual(
            f.baht_text(Decimal("1234.50")), "หนึ่งพันสองร้อยสามสิบสี่บาทห้าสิบสตางค์"
        )

    def test_more_place_rules(self):
        cases = {
            10: "สิบบาทถ้วน",
            12: "สิบสองบาทถ้วน",
            20: "ยี่สิบบาทถ้วน",
            22: "ยี่สิบสองบาทถ้วน",
            31: "สามสิบเอ็ดบาทถ้วน",
            110: "หนึ่งร้อยสิบบาทถ้วน",
            111: "หนึ่งร้อยสิบเอ็ดบาทถ้วน",
            1001: "หนึ่งพันเอ็ดบาทถ้วน",
            1_010_000: "หนึ่งล้านหนึ่งหมื่นบาทถ้วน",
            1_100_000: "หนึ่งล้านหนึ่งแสนบาทถ้วน",
            11_000_000: "สิบเอ็ดล้านบาทถ้วน",
            21_000_021: "ยี่สิบเอ็ดล้านยี่สิบเอ็ดบาทถ้วน",
            101_000_000: "หนึ่งร้อยเอ็ดล้านบาทถ้วน",
        }
        for baht, expected in cases.items():
            with self.subTest(baht=baht):
                self.assertEqual(f.baht_text(baht), expected)

    def test_satang_words(self):
        self.assertEqual(f.baht_text("0.10"), "ศูนย์บาทสิบสตางค์")
        self.assertEqual(f.baht_text("0.11"), "ศูนย์บาทสิบเอ็ดสตางค์")
        self.assertEqual(f.baht_text("5.21"), "ห้าบาทยี่สิบเอ็ดสตางค์")
        self.assertEqual(f.baht_text("5.99"), "ห้าบาทเก้าสิบเก้าสตางค์")

    def test_rounds_half_up_to_the_satang(self):
        self.assertEqual(f.baht_text("0.005"), "ศูนย์บาทหนึ่งสตางค์")
        self.assertEqual(f.baht_text("0.004"), "ศูนย์บาทถ้วน")
        self.assertEqual(f.baht_text("1.995"), "สองบาทถ้วน")
        self.assertEqual(f.baht_text(Decimal("2.345")), "สองบาทสามสิบห้าสตางค์")
        # Away from zero for negatives, as ROUND_HALF_UP does.
        self.assertEqual(f.baht_text("-0.005"), "ลบศูนย์บาทหนึ่งสตางค์")

    def test_float_input_uses_its_shortest_repr(self):
        # float(1.005) is 1.00499999...; read as written it rounds up.
        self.assertEqual(f.baht_text(1.005), "หนึ่งบาทหนึ่งสตางค์")
        self.assertEqual(f.baht_text(35140.905000000006), f.baht_text("35140.91"))
        self.assertEqual(f.baht_text(0.1 + 0.2), "ศูนย์บาทสามสิบสตางค์")

    def test_negative_zero_has_no_minus(self):
        self.assertEqual(f.baht_text("-0.001"), "ศูนย์บาทถ้วน")
        self.assertEqual(f.baht_text(-1), "ลบหนึ่งบาทถ้วน")

    def test_strings_with_grouping(self):
        self.assertEqual(f.baht_text("1,234.50"), f.baht_text("1234.5"))

    def test_rejects_junk(self):
        for bad in (None, "", "abc", float("nan"), float("inf"), Decimal("NaN"), True, [1]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    f.baht_text(bad)

    def test_number_text(self):
        self.assertEqual(f.number_text(0), "ศูนย์")
        self.assertEqual(f.number_text(1), "หนึ่ง")
        self.assertEqual(f.number_text(11), "สิบเอ็ด")
        with self.assertRaises(ValueError):
            f.number_text(-1)


class BuddhistEraTests(SimpleTestCase):
    def test_long_and_short(self):
        d = datetime.date(2026, 11, 14)
        self.assertEqual(f.be_date(d), "14 พฤศจิกายน 2569")
        self.assertEqual(f.be_date(d, "short"), "14 พ.ย. 69")

    def test_every_month(self):
        for month in range(1, 13):
            d = datetime.date(2026, month, 1)
            self.assertEqual(f.be_date(d), f"1 {f.THAI_MONTHS[month - 1]} 2569")
            self.assertEqual(
                f.be_date(d, "short"), f"1 {f.THAI_MONTHS_SHORT[month - 1]} 69"
            )

    def test_short_year_keeps_two_digits(self):
        self.assertEqual(f.be_date(datetime.date(1957, 1, 2), "short"), "2 ม.ค. 00")
        self.assertEqual(f.be_date(datetime.date(1966, 1, 2), "short"), "2 ม.ค. 09")

    def test_inputs(self):
        self.assertEqual(f.be_date(datetime.datetime(2026, 1, 31, 23, 0)), "31 มกราคม 2569")
        self.assertEqual(f.be_date("2026-02-28"), "28 กุมภาพันธ์ 2569")
        self.assertEqual(f.be_date(None), "")
        self.assertEqual(f.be_date("not a date"), "")
        self.assertEqual(f.be_date(12345), "")

    def test_be_year(self):
        self.assertEqual(f.be_year(2026), 2569)
        self.assertEqual(f.be_year(datetime.date(2026, 5, 1)), 2569)
        self.assertEqual(f.be_year("2025-12-31"), 2568)
        self.assertIsNone(f.be_year(None))
        self.assertIsNone(f.be_year(True))


class ThaiIdTests(SimpleTestCase):
    ID = "1234567890121"

    def test_format(self):
        self.assertEqual(f.format_thai_id(self.ID), "1-2345-67890-12-1")
        self.assertEqual(f.format_thai_id("1 2345 67890 12 1"), "1-2345-67890-12-1")
        self.assertEqual(f.format_thai_id("1-2345-67890-12-1"), "1-2345-67890-12-1")

    def test_format_leaves_other_values_alone(self):
        self.assertEqual(f.format_thai_id("12345"), "12345")
        self.assertEqual(f.format_thai_id("12345678901ab"), "12345678901ab")
        self.assertEqual(f.format_thai_id(None), "")
        self.assertEqual(f.format_thai_id(""), "")

    def test_mask(self):
        self.assertEqual(f.mask_thai_id(self.ID), "X-XXXX-XXXX0-12-1")
        self.assertEqual(f.mask_thai_id("1-2345-67890-12-1"), "X-XXXX-XXXX0-12-1")
        self.assertNotIn("2345", f.mask_thai_id(self.ID))

    def test_mask_other_values(self):
        self.assertEqual(f.mask_thai_id("12345"), "X2345")
        self.assertEqual(f.mask_thai_id("12"), "12")
        self.assertEqual(f.mask_thai_id(None), "")
        self.assertEqual(f.mask_thai_id(""), "")
