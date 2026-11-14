"""
The OrvaHR payroll engine hook in compute_yearly_tax.

The engine is a separate Rust service, so these tests replace the single HTTP
call (``orvahr_engine._post``) with a fake that answers the way the engine
does. What is tested here is the *policy*: off changes nothing, shadow never
changes a payslip, active uses the engine and fails loudly without it.
"""

from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from payroll.methods import orvahr_engine
from payroll.methods.tax_calc import TaxComputationError, compute_yearly_tax
from payroll.models.models import FilingStatus
from payroll.tax_packs import TAX_PACKS, load_tax_packs

THAI = "Thailand — Personal Income Tax (2026)"
ON = dict(
    ORVAHR_PAYROLL_ENGINE_URL="http://engine.invalid:8090",
    ORVAHR_PAYROLL_ENGINE_FILING_STATUSES=[THAI],
)


def engine_says(tax_baht):
    """A fake of the engine's /v1/tax/annual answer, in satang."""
    return mock.patch.object(
        orvahr_engine, "_post", return_value={"tax": round(tax_baht * 100)}
    )


def engine_down():
    return mock.patch.object(
        orvahr_engine, "_post", side_effect=orvahr_engine.EngineUnavailable("down")
    )


class ThaiPackTests(TestCase):
    def setUp(self):
        load_tax_packs(["th_2026"])
        self.filing = FilingStatus.objects.get(filing_status=THAI)

    def test_600k_matches_the_engines_own_reference_figure(self):
        # Same case as the Rust engine's annual_tax_single_person_600k test.
        tax, _ = compute_yearly_tax(self.filing, 600000)
        self.assertAlmostEqual(tax, 21500.0, places=2)

    def test_under_the_threshold_pays_nothing(self):
        tax, _ = compute_yearly_tax(self.filing, 200000)
        self.assertEqual(tax, 0)

    def test_pack_is_listed(self):
        self.assertIn("th_2026", [p["key"] for p in TAX_PACKS])


@override_settings(**ON, ORVAHR_PAYROLL_ENGINE_MODE="off")
class OffTests(TestCase):
    def test_off_never_calls_the_engine(self):
        load_tax_packs(["th_2026"])
        filing = FilingStatus.objects.get(filing_status=THAI)
        with engine_says(1) as post:
            tax, breakdown = compute_yearly_tax(filing, 600000)
        post.assert_not_called()
        self.assertAlmostEqual(tax, 21500.0, places=2)
        self.assertNotIn("engine", breakdown)


@override_settings(**ON, ORVAHR_PAYROLL_ENGINE_MODE="shadow")
class ShadowTests(TestCase):
    def setUp(self):
        load_tax_packs(["th_2026", "flat_10"])
        self.filing = FilingStatus.objects.get(filing_status=THAI)

    def test_agreement_is_silent_and_changes_nothing(self):
        with engine_says(21500) as post, self.assertNoLogs(
            "payroll.methods.tax_calc", level="WARNING"
        ):
            tax, breakdown = compute_yearly_tax(self.filing, 600000)
        post.assert_called_once()
        self.assertAlmostEqual(tax, 21500.0, places=2)
        self.assertEqual(breakdown["engine"], {"mode": "shadow", "tax": 21500.0})

    def test_disagreement_is_logged_but_the_existing_tax_is_still_paid(self):
        with engine_says(99999), self.assertLogs(
            "payroll.methods.tax_calc", level="WARNING"
        ) as logs:
            tax, _ = compute_yearly_tax(self.filing, 600000)
        self.assertAlmostEqual(tax, 21500.0, places=2)
        self.assertIn("disagrees", logs.output[0])

    def test_an_engine_outage_never_breaks_payroll_in_shadow(self):
        with engine_down():
            tax, _ = compute_yearly_tax(self.filing, 600000)
        self.assertAlmostEqual(tax, 21500.0, places=2)

    def test_a_filing_status_not_listed_is_never_sent(self):
        other = FilingStatus.objects.get(filing_status="Flat 10%")
        with engine_says(1) as post:
            compute_yearly_tax(other, 600000)
        post.assert_not_called()


@override_settings(**ON, ORVAHR_PAYROLL_ENGINE_MODE="active")
class ActiveTests(TestCase):
    def setUp(self):
        load_tax_packs(["th_2026"])
        self.filing = FilingStatus.objects.get(filing_status=THAI)

    def test_the_engines_figure_is_used(self):
        with engine_says(12345):
            tax, breakdown = compute_yearly_tax(self.filing, 600000)
        self.assertAlmostEqual(tax, 12345.0, places=2)
        self.assertEqual(breakdown["engine"]["mode"], "active")

    def test_an_outage_fails_loudly_instead_of_paying_the_old_number(self):
        with engine_down(), self.assertRaises(TaxComputationError):
            compute_yearly_tax(self.filing, 600000)


class ModeTests(SimpleTestCase):
    def test_no_url_means_off_whatever_the_mode(self):
        with override_settings(
            ORVAHR_PAYROLL_ENGINE_URL="", ORVAHR_PAYROLL_ENGINE_MODE="active"
        ):
            self.assertEqual(orvahr_engine.mode(), "off")

    def test_an_unknown_mode_is_treated_as_off(self):
        with override_settings(ORVAHR_PAYROLL_ENGINE_MODE="turbo", **ON):
            with self.assertLogs("payroll.methods.orvahr_engine", level="ERROR"):
                self.assertEqual(orvahr_engine.mode(), "off")

    def test_a_non_http_url_is_refused(self):
        with override_settings(ORVAHR_PAYROLL_ENGINE_URL="file:///etc/passwd"):
            with self.assertRaises(orvahr_engine.EngineUnavailable):
                orvahr_engine._post("/x", {})

    def test_tolerance(self):
        self.assertTrue(orvahr_engine.agrees(100.0, 100.4))
        self.assertFalse(orvahr_engine.agrees(100.0, 101.0))
