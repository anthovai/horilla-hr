from decimal import Decimal as D

from django.test import SimpleTestCase

from orvahr_billing.money import line_amount, q, split_vat_inclusive, vat_on, wht_on


class MoneyTests(SimpleTestCase):
    def test_half_up_not_bankers(self):
        self.assertEqual(q(D("0.105")), D("0.11"))
        self.assertEqual(q(D("0.125")), D("0.13"))  # banker's rounding would give 0.12
        self.assertEqual(q(D("-0.005")), D("-0.01"))

    def test_vat(self):
        self.assertEqual(vat_on(D("1.50"), D("7")), D("0.11"))  # 0.105
        self.assertEqual(vat_on(D("1000"), D("7.00")), D("70.00"))
        self.assertEqual(vat_on(D("0.07"), D("7")), D("0.00"))

    def test_wht_is_on_the_value_excluding_vat(self):
        self.assertEqual(wht_on(D("1234.50"), 3), D("37.04"))  # 37.035
        self.assertEqual(wht_on(D("1000.00"), D("3")), D("30.00"))

    def test_split_inclusive_sums_exactly(self):
        self.assertEqual(split_vat_inclusive(D("107.00"), 7), (D("100.00"), D("7.00")))
        base, vat = split_vat_inclusive(D("100.00"), D("7"))
        self.assertEqual((base, vat), (D("93.46"), D("6.54")))
        for gross in ("0.01", "1.00", "999.99", "12345.67"):
            base, vat = split_vat_inclusive(D(gross), 7)
            self.assertEqual(base + vat, D(gross))
        self.assertEqual(split_vat_inclusive(D("50"), 0), (D("50.00"), D("0.00")))

    def test_line_amount(self):
        self.assertEqual(line_amount(D("3"), D("33.335")), D("100.01"))  # 100.005
        self.assertEqual(line_amount(12, D("49.00")), D("588.00"))
