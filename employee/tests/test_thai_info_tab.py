"""
Thai Info tab on the employee profile: who can see it, masking, and the HR
edit flows for the Thai profile and tax claims. All IDs are synthetic.
"""

from django.contrib.auth.models import Permission
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from employee.models import EmployeeTaxClaim, EmployeeThaiProfile
from employee.thai_info import current_tax_year
from employee.thai_validators import format_thai_id, thai_national_id_checksum_digit
from horilla.testkit import make_company, make_employee, make_user

HX = {"HTTP_HX_REQUEST": "true"}


def synthetic_id(first_twelve):
    return first_twelve + str(thai_national_id_checksum_digit(first_twelve))


NID = synthetic_id("123456789012")  # 1234567890121
NID_DASHED = "1-2345-67890-12-1"
NID_MASKED = "1-XXXX-XXXXX-12-1"
NID2 = synthetic_id("310000000002")
SSO = "5555544444333"
PROMPTPAY = "0812345678"


class FormatThaiIdTests(SimpleTestCase):
    def test_masked_and_full(self):
        self.assertEqual(NID, "1234567890121")
        self.assertEqual(format_thai_id(NID), NID_MASKED)
        self.assertEqual(format_thai_id(NID, masked=False), NID_DASHED)
        self.assertEqual(format_thai_id(None), "")


class ThaiInfoTabBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company("Thai Tab Co")
        cls.hr_user = make_user("thai_hr")
        cls.hr = make_employee(
            company=cls.company,
            email="hr@thai.test",
            phone="9300000001",
            user=cls.hr_user,
        )
        for codename in ("view_employee", "change_employee"):
            cls.hr_user.user_permissions.add(Permission.objects.get(codename=codename))

        cls.self_user = make_user("thai_self")
        cls.target = make_employee(
            company=cls.company,
            email="self@thai.test",
            phone="9300000002",
            user=cls.self_user,
        )
        cls.other_user = make_user("thai_other")
        cls.other = make_employee(
            company=cls.company,
            email="other@thai.test",
            phone="9300000003",
            user=cls.other_user,
        )
        cls.viewer_user = make_user("thai_viewer")
        make_employee(
            company=cls.company,
            email="viewer@thai.test",
            phone="9300000004",
            user=cls.viewer_user,
        )
        cls.viewer_user.user_permissions.add(
            Permission.objects.get(codename="view_employee")
        )

    def setUp(self):
        self.profile = EmployeeThaiProfile.objects.create(
            employee_id=self.target,
            name_title="นาย",
            first_name_th="สมชาย",
            last_name_th="ใจดี",
            national_id=NID,
            social_security_no=SSO,
            promptpay_id=PROMPTPAY,
            bank_code="004",
            province="กรุงเทพมหานคร",
            postcode="10110",
        )
        self.year = current_tax_year()
        self.tab_url = reverse("employee-thai-info-tab", args=[self.target.pk])

    def get_tab(self, user, **params):
        self.client.force_login(user)
        return self.client.get(self.tab_url, params, **HX)

    def assertNoFullIds(self, response):
        body = response.content.decode()
        for secret in (NID, NID_DASHED, SSO, PROMPTPAY):
            self.assertNotIn(secret, body)


class ThaiInfoTabVisibilityTests(ThaiInfoTabBase):
    def test_hr_sees_full_ids_and_edit_controls(self):
        response = self.get_tab(self.hr_user)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn(NID_DASHED, body)
        self.assertIn(SSO, body)
        self.assertIn("สมชาย", body)
        self.assertIn(
            reverse("employee-thai-profile-edit", args=[self.target.pk]), body
        )
        self.assertIn(reverse("employee-tax-claim-add", args=[self.target.pk]), body)

    def test_self_sees_own_data_masked_without_edit(self):
        response = self.get_tab(self.self_user)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn(NID_MASKED, body)
        self.assertIn("สมชาย", body)
        self.assertNoFullIds(response)
        self.assertNotIn(
            reverse("employee-thai-profile-edit", args=[self.target.pk]), body
        )

    def test_view_only_hr_sees_masked(self):
        response = self.get_tab(self.viewer_user)
        self.assertEqual(response.status_code, 200)
        self.assertIn(NID_MASKED, response.content.decode())
        self.assertNoFullIds(response)

    def test_other_employee_forbidden(self):
        response = self.get_tab(self.other_user)
        self.assertEqual(response.status_code, 403)
        self.assertNoFullIds(response)

    def test_other_employee_cannot_edit_or_add(self):
        self.client.force_login(self.other_user)
        edit = reverse("employee-thai-profile-edit", args=[self.target.pk])
        self.assertEqual(self.client.get(edit, **HX).status_code, 403)
        response = self.client.post(edit, {"national_id": NID2}, **HX)
        self.assertEqual(response.status_code, 403)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.national_id, NID)

    def test_self_cannot_edit_own_profile(self):
        self.client.force_login(self.self_user)
        edit = reverse("employee-thai-profile-edit", args=[self.target.pk])
        response = self.client.get(edit, **HX)
        self.assertEqual(response.status_code, 403)
        self.assertNoFullIds(response)

    def test_tab_registered_on_profile_and_visible_to_self(self):
        from employee.cbv.employee_profile import EmployeeProfileView

        from employee.cbv.accessibility import thai_info_accessibility

        tab = next(
            t for t in EmployeeProfileView.tabs if str(t["title"]) == "Thai Info"
        )
        self.assertEqual(
            tab["accessibility"], "employee.cbv.accessibility.thai_info_accessibility"
        )

        class Req:
            def __init__(self, user):
                self.user = user

        for user, expected in (
            (self.hr_user, True),
            (self.viewer_user, True),
            (self.self_user, True),
            (self.other_user, False),
        ):
            self.assertEqual(
                thai_info_accessibility(Req(user), self.target), expected, user
            )

        # The profile page loads the tab through the shared hzp-tab route.
        url = tab["url"].format(pk=self.target.pk)
        self.client.force_login(self.self_user)
        response = self.client.get(url, **HX)
        self.assertEqual(response.status_code, 200)
        self.assertIn(NID_MASKED, response.content.decode())
        self.assertNoFullIds(response)
        self.client.force_login(self.other_user)
        self.assertEqual(self.client.get(url, **HX).status_code, 403)

    def test_requires_htmx(self):
        self.client.force_login(self.hr_user)
        self.assertEqual(self.client.get(self.tab_url).status_code, 405)


class ThaiProfileEditTests(ThaiInfoTabBase):
    def edit_url(self, employee):
        return reverse("employee-thai-profile-edit", args=[employee.pk])

    def test_hr_creates_profile(self):
        self.client.force_login(self.hr_user)
        response = self.client.post(
            self.edit_url(self.other),
            {
                "name_title": "นางสาว",
                "first_name_th": "สมหญิง",
                "national_id": "3-1000-00000-02-" + NID2[-1],
                "postcode": "50000",
                "promptpay_id": "0898765432",
                "bank_code": "014",
            },
            **HX,
        )
        self.assertEqual(response.status_code, 200)
        profile = EmployeeThaiProfile._base_manager.get(employee_id=self.other)
        self.assertEqual(profile.national_id, NID2)
        self.assertEqual(profile.tax_id, NID2)  # defaults to national ID
        self.assertEqual(profile.promptpay_id, "0898765432")
        self.assertTemplateUsed(response, "tabs/thai_info_tab.html")

    def test_hr_updates_profile(self):
        self.client.force_login(self.hr_user)
        response = self.client.get(self.edit_url(self.target), **HX)
        self.assertEqual(response.status_code, 200)
        response = self.client.post(
            self.edit_url(self.target),
            {
                "name_title": "นาย",
                "first_name_th": "สมชาย",
                "last_name_th": "รักไทย",
                "national_id": NID,
                "social_security_no": SSO,
                "postcode": "10120",
            },
            **HX,
        )
        self.assertEqual(response.status_code, 200)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.last_name_th, "รักไทย")
        self.assertEqual(self.profile.postcode, "10120")
        self.assertIsNone(self.profile.promptpay_id)

    def test_invalid_checksum_rejected(self):
        self.client.force_login(self.hr_user)
        bad = NID[:12] + str((int(NID[12]) + 1) % 10)
        response = self.client.post(
            self.edit_url(self.other), {"national_id": bad}, **HX
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "tabs/thai_info_profile_form.html")
        self.assertIn("check digit does not match", response.content.decode())
        self.assertFalse(
            EmployeeThaiProfile._base_manager.filter(employee_id=self.other).exists()
        )

    def test_bad_formats_rejected(self):
        self.client.force_login(self.hr_user)
        response = self.client.post(
            self.edit_url(self.other),
            {"national_id": "12345", "postcode": "1011", "promptpay_id": "12345"},
            **HX,
        )
        form = response.context["form"]
        self.assertIn("national_id", form.errors)
        self.assertIn("postcode", form.errors)
        self.assertIn("promptpay_id", form.errors)
        self.assertFalse(
            EmployeeThaiProfile._base_manager.filter(employee_id=self.other).exists()
        )

    def test_duplicate_national_id_rejected(self):
        self.client.force_login(self.hr_user)
        response = self.client.post(
            self.edit_url(self.other), {"national_id": NID}, **HX
        )
        self.assertIn("national_id", response.context["form"].errors)
        self.assertFalse(
            EmployeeThaiProfile._base_manager.filter(employee_id=self.other).exists()
        )


class TaxClaimEditTests(ThaiInfoTabBase):
    def add_url(self):
        return reverse("employee-tax-claim-add", args=[self.target.pk])

    def test_add_edit_delete_claim(self):
        self.client.force_login(self.hr_user)
        response = self.client.post(
            self.add_url(),
            {"tax_year": self.year, "code": "child", "units": 2},
            **HX,
        )
        self.assertEqual(response.status_code, 200)
        claim = EmployeeTaxClaim.objects.get(employee_id=self.target)
        self.assertEqual(claim.units, 2)
        self.assertIn(claim.get_code_display(), response.content.decode())

        edit_url = reverse("employee-tax-claim-edit", args=[claim.pk])
        self.assertEqual(self.client.get(edit_url, **HX).status_code, 200)
        response = self.client.post(
            edit_url, {"tax_year": self.year, "code": "child", "units": 3}, **HX
        )
        self.assertEqual(response.status_code, 200)
        claim.refresh_from_db()
        self.assertEqual(claim.units, 3)

        delete_url = reverse("employee-tax-claim-delete", args=[claim.pk])
        self.assertEqual(self.client.get(delete_url, **HX).status_code, 405)
        response = self.client.post(delete_url, **HX)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(EmployeeTaxClaim.objects.filter(pk=claim.pk).exists())

    def test_capped_claim_needs_amount_not_units(self):
        self.client.force_login(self.hr_user)
        response = self.client.post(
            self.add_url(), {"tax_year": self.year, "code": "rmf", "units": 1}, **HX
        )
        errors = response.context["form"].errors
        self.assertIn("amount", errors)
        self.assertFalse(EmployeeTaxClaim.objects.exists())

    def test_per_unit_claim_rejects_amount(self):
        self.client.force_login(self.hr_user)
        response = self.client.post(
            self.add_url(),
            {"tax_year": self.year, "code": "parent", "units": 1, "amount": 500},
            **HX,
        )
        self.assertIn("amount", response.context["form"].errors)
        self.assertFalse(EmployeeTaxClaim.objects.exists())

    def test_duplicate_code_same_year_rejected(self):
        EmployeeTaxClaim.objects.create(
            employee_id=self.target, tax_year=self.year, code="rmf", amount=1000
        )
        self.client.force_login(self.hr_user)
        response = self.client.post(
            self.add_url(), {"tax_year": self.year, "code": "rmf", "amount": 2000}, **HX
        )
        self.assertIn("code", response.context["form"].errors)
        self.assertEqual(EmployeeTaxClaim.objects.count(), 1)

    def test_buddhist_year_rejected(self):
        self.client.force_login(self.hr_user)
        response = self.client.post(
            self.add_url(), {"tax_year": 2569, "code": "spouse", "units": 1}, **HX
        )
        self.assertIn("tax_year", response.context["form"].errors)

    def test_tab_shows_selected_year_only(self):
        EmployeeTaxClaim.objects.create(
            employee_id=self.target, tax_year=self.year, code="spouse", units=1
        )
        EmployeeTaxClaim.objects.create(
            employee_id=self.target, tax_year=self.year - 1, code="rmf", amount=5000
        )
        response = self.get_tab(self.hr_user)
        self.assertEqual([c.code for c in response.context["claims"]], ["spouse"])
        response = self.get_tab(self.hr_user, year=self.year - 1)
        self.assertEqual([c.code for c in response.context["claims"]], ["rmf"])

    def test_self_sees_claims_but_cannot_change_them(self):
        claim = EmployeeTaxClaim.objects.create(
            employee_id=self.target, tax_year=self.year, code="spouse", units=1
        )
        response = self.get_tab(self.self_user)
        self.assertEqual(list(response.context["claims"]), [claim])
        self.assertNotIn(
            reverse("employee-tax-claim-delete", args=[claim.pk]),
            response.content.decode(),
        )
        for url in (
            self.add_url(),
            reverse("employee-tax-claim-edit", args=[claim.pk]),
            reverse("employee-tax-claim-delete", args=[claim.pk]),
        ):
            response = self.client.post(
                url, {"tax_year": self.year, "code": "spouse", "units": 1}, **HX
            )
            self.assertEqual(response.status_code, 403, url)
        self.assertTrue(EmployeeTaxClaim.objects.filter(pk=claim.pk).exists())
