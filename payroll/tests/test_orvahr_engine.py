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


def _routing_post(tax_baht=1000, sso_employee_satang=75000):
    """Fake of _post answering both /v1/tax/annual and /v1/sso."""

    def fake(path, payload):
        if path == "/v1/sso":
            return {"base": payload["wage"], "employee": sso_employee_satang, "employer": sso_employee_satang}
        return {"tax": round(tax_baht * 100)}

    return mock.patch.object(orvahr_engine, "_post", side_effect=fake)


def _annual_payload(post):
    calls = [c for c in post.call_args_list if c.args[0] == "/v1/tax/annual"]
    assert len(calls) == 1, post.call_args_list
    return calls[0].args[1]


class ClaimsForTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from horilla.testkit import make_company, make_employee

        company = make_company("Engine Claim Co")
        cls.e1 = make_employee(company=company, email="e1@engine.test", phone="9300000001")
        cls.e2 = make_employee(company=company, email="e2@engine.test", phone="9300000002")
        cls.add(cls.e1, 2026, "child", units=2)
        cls.add(cls.e1, 2026, "life_insurance", amount=12345.67)
        cls.add(cls.e1, 2025, "spouse", units=1)
        cls.add(cls.e2, 2026, "rmf", amount=999)

    @classmethod
    def add(cls, emp, year, code, **kw):
        from employee.models import EmployeeTaxClaim

        EmployeeTaxClaim.objects.create(employee_id=emp, tax_year=year, code=code, **kw)

    def test_units_and_amounts_in_engine_shape_baht_to_satang(self):
        self.assertEqual(
            orvahr_engine.claims_for(self.e1, 2026),
            [
                {"code": "child", "units": 2},
                {"code": "life_insurance", "amount": 1234567},
            ],
        )

    def test_year_filtering_and_employee_isolation(self):
        self.assertEqual(orvahr_engine.claims_for(self.e1, 2025), [{"code": "spouse", "units": 1}])
        self.assertEqual(orvahr_engine.claims_for(self.e2, 2026), [{"code": "rmf", "amount": 99900}])
        self.assertEqual(orvahr_engine.claims_for(self.e2, 2025), [])

    def test_accepts_an_employee_id(self):
        self.assertEqual(len(orvahr_engine.claims_for(self.e1.pk, 2026)), 2)


class HookClaimsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        from employee.models import EmployeeTaxClaim
        from horilla.testkit import make_company, make_employee

        load_tax_packs(["th_2026"])
        company = make_company("Hook Co")
        cls.emp = make_employee(company=company, email="h1@engine.test", phone="9400000001")
        EmployeeTaxClaim.objects.create(employee_id=cls.emp, tax_year=2026, code="child", units=1)

    def setUp(self):
        self.filing = FilingStatus.objects.get(filing_status=THAI)

    @override_settings(**ON, ORVAHR_PAYROLL_ENGINE_MODE="shadow")
    def test_shadow_sends_claims_and_does_not_warn(self):
        with _routing_post(5000) as post, self.assertNoLogs(
            "payroll.methods.tax_calc", level="WARNING"
        ):
            tax, breakdown = compute_yearly_tax(self.filing, 600000, employee=self.emp, tax_year=2026)
        self.assertEqual(_annual_payload(post)["claims"], [{"code": "child", "units": 1}])
        self.assertAlmostEqual(tax, 21500.0, places=2)  # existing figure still paid
        self.assertEqual(breakdown["engine"]["tax"], 5000.0)
        self.assertEqual(breakdown["engine"]["claims"], [{"code": "child", "units": 1}])

    @override_settings(**ON, ORVAHR_PAYROLL_ENGINE_MODE="shadow")
    def test_shadow_with_claims_logs_info(self):
        with _routing_post(5000), self.assertLogs("payroll.methods.tax_calc", level="INFO") as logs:
            compute_yearly_tax(self.filing, 600000, employee=self.emp, tax_year=2026)
        self.assertTrue(all(r.levelname == "INFO" for r in logs.records))

    @override_settings(**ON, ORVAHR_PAYROLL_ENGINE_MODE="shadow")
    def test_shadow_without_claims_still_warns_on_disagreement(self):
        with _routing_post(99999), self.assertLogs("payroll.methods.tax_calc", level="WARNING"):
            compute_yearly_tax(self.filing, 600000, employee=self.emp, tax_year=2030)

    @override_settings(**ON, ORVAHR_PAYROLL_ENGINE_MODE="active")
    def test_active_pays_the_engines_figure_with_claims(self):
        with _routing_post(4321) as post:
            tax, _ = compute_yearly_tax(self.filing, 600000, employee=self.emp, tax_year=2026)
        self.assertAlmostEqual(tax, 4321.0, places=2)
        self.assertEqual(_annual_payload(post)["claims"], [{"code": "child", "units": 1}])

    @override_settings(**ON, ORVAHR_PAYROLL_ENGINE_MODE="active")
    def test_no_employee_sends_no_claims_and_no_sso(self):
        with _routing_post(100) as post:
            compute_yearly_tax(self.filing, 600000)
        payload = _annual_payload(post)
        self.assertEqual(payload["claims"], [])
        self.assertEqual(payload["sso_paid_year"], 0)
        self.assertEqual(len(post.call_args_list), 1)

    @override_settings(**ON, ORVAHR_PAYROLL_ENGINE_MODE="shadow")
    def test_no_tax_year_sends_no_claims(self):
        with _routing_post(100) as post:
            compute_yearly_tax(self.filing, 600000, employee=self.emp)
        self.assertEqual(_annual_payload(post)["claims"], [])

    @override_settings(**ON, ORVAHR_PAYROLL_ENGINE_MODE="active")
    def test_sso_projected_only_for_enrolled_employee(self):
        from employee.models import EmployeeThaiProfile

        with _routing_post(100) as post:
            compute_yearly_tax(self.filing, 600000, employee=self.emp, tax_year=2026)
        self.assertEqual(_annual_payload(post)["sso_paid_year"], 0)
        EmployeeThaiProfile.objects.create(employee_id=self.emp, social_security_no="1234567890123")
        with _routing_post(100, sso_employee_satang=75000) as post:
            compute_yearly_tax(self.filing, 600000, employee=self.emp, tax_year=2026)
        sso = [c for c in post.call_args_list if c.args[0] == "/v1/sso"]
        # 600000/12 baht in satang, with the rules chosen by the tax year.
        self.assertEqual(sso[0].args[1], {"wage": 5000000, "as_of": "2026-12-31"})
        self.assertEqual(_annual_payload(post)["sso_paid_year"], 750 * 12 * 100)

    @override_settings(**ON, ORVAHR_PAYROLL_ENGINE_MODE="off")
    def test_off_never_touches_claims_or_the_database_for_them(self):
        base = self._base_queries()
        with _routing_post() as post, mock.patch.object(
            orvahr_engine, "claims_for"
        ) as cf, self.assertNumQueries(base):
            compute_yearly_tax(self.filing, 600000, employee=self.emp, tax_year=2026)
        post.assert_not_called()
        cf.assert_not_called()

    def _base_queries(self):
        # What the existing computation queries on its own (no employee).
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            compute_yearly_tax(self.filing, 600000)
        return len(ctx)


class AsOfTests(SimpleTestCase):
    """The rule set must follow the tax year, not today's date."""

    def test_as_of_is_the_last_day_of_the_tax_year(self):
        self.assertEqual(orvahr_engine.as_of_for_year(2025), "2025-12-31")
        self.assertIsNone(orvahr_engine.as_of_for_year(None))

    @override_settings(**ON)
    def test_annual_tax_sends_as_of_only_when_given(self):
        with mock.patch.object(orvahr_engine, "_post", return_value={"tax": 0}) as post:
            orvahr_engine.annual_tax(100000)
            self.assertNotIn("as_of", post.call_args.args[1])
            orvahr_engine.annual_tax(100000, as_of="2025-12-31")
            self.assertEqual(post.call_args.args[1]["as_of"], "2025-12-31")


@override_settings(**ON, ORVAHR_PAYROLL_ENGINE_MODE="active")
class AsOfHookTests(TestCase):
    def setUp(self):
        load_tax_packs(["th_2026"])
        self.filing = FilingStatus.objects.get(filing_status=THAI)

    def test_the_hook_selects_rules_by_the_tax_year(self):
        with engine_says(1000) as post:
            compute_yearly_tax(self.filing, 600000, tax_year=2025)
        self.assertEqual(post.call_args.args[1]["as_of"], "2025-12-31")

    def test_without_a_tax_year_the_engine_default_applies(self):
        with engine_says(1000) as post:
            compute_yearly_tax(self.filing, 600000)
        self.assertNotIn("as_of", post.call_args.args[1])
