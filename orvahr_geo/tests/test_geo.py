"""Thai geography: fixture, loader, JSON endpoints and the Thai Info form."""

from io import StringIO

from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from employee.forms import EmployeeThaiProfileForm
from employee.models import EmployeeThaiProfile
from horilla.testkit import make_company, make_employee, make_user
from orvahr_geo.loader import bump_cache_version, load_th_geo, read_fixture
from orvahr_geo.lookup import find_subdistrict, normalize_name
from orvahr_geo.models import District, Province, Subdistrict

HX = {"HTTP_HX_REQUEST": "true"}


class FixtureTests(SimpleTestCase):
    def test_counts_and_postcodes(self):
        data = read_fixture()
        self.assertEqual(len(data["provinces"]), 77)
        self.assertEqual(len(data["districts"]), 928)
        self.assertEqual(len(data["subdistricts"]), 7436)
        for row in data["subdistricts"]:
            self.assertTrue(row[3].split(), row)
            for postcode in row[3].split():
                self.assertRegex(postcode, r"^\d{5}$")

    def test_codes_chain(self):
        data = read_fixture()
        provinces = {r[0] for r in data["provinces"]}
        districts = {r[0] for r in data["districts"]}
        self.assertTrue(all(d[:2] in provinces for d in districts))
        self.assertTrue(all(s[0][:4] in districts for s in data["subdistricts"]))


class LoaderTests(TestCase):
    """The 0002 migration already loaded the fixture into the test DB."""

    def test_migration_loaded_everything(self):
        self.assertEqual(Province.objects.count(), 77)
        self.assertEqual(District.objects.count(), 928)
        self.assertEqual(Subdistrict.objects.count(), 7436)
        self.assertFalse(
            Subdistrict.objects.exclude(postcode__regex=r"^\d{5}$").exists()
        )
        bangkok = Province.objects.get(code="10")
        self.assertEqual(bangkok.iso_code, "TH-10")
        self.assertEqual(bangkok.name_en, "Bangkok")
        self.assertEqual(Province.objects.get(name_en="Bueng Kan").iso_code, "TH-38")

    def test_rerun_changes_nothing(self):
        result = load_th_geo(Province, District, Subdistrict)
        self.assertEqual(
            result,
            {
                "provinces": (0, 0, 0),
                "districts": (0, 0, 0),
                "subdistricts": (0, 0, 0),
            },
        )
        self.assertEqual(Subdistrict.objects.count(), 7436)

    def test_restores_edited_and_deleted_rows(self):
        sub = Subdistrict.objects.get(code="100101")
        sub.name_en = "Changed"
        sub.postcode = "99999"
        sub.save()
        Subdistrict.objects.filter(code="100102").delete()
        call_command("load_th_geo", stdout=StringIO())
        sub.refresh_from_db()
        self.assertEqual(sub.name_en, "Phra Borom Maha Ratchawang")
        self.assertEqual(sub.postcode, "10200")
        self.assertTrue(Subdistrict.objects.filter(code="100102").exists())
        self.assertEqual(Subdistrict.objects.count(), 7436)
        self.assertEqual(
            load_th_geo(Province, District, Subdistrict)["subdistricts"], (0, 0, 0)
        )

    def test_multi_postcode_subdistrict(self):
        sub = Subdistrict.objects.get(code="100508")  # Tha Raeng
        self.assertEqual(sub.postcode, "10220")
        self.assertEqual(sub.postcode_list, ["10220", "10230"])


class EndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        company = make_company("Geo Co")
        cls.user = make_user("geo_user")
        make_employee(
            company=company, email="geo@geo.test", phone="9400000001", user=cls.user
        )

    def setUp(self):
        cache.clear()
        self.client.force_login(self.user)

    def get(self, name, **params):
        return self.client.get(reverse(f"orvahr_geo:{name}"), params)

    def test_login_required(self):
        self.client.logout()
        response = self.get("provinces")
        self.assertEqual(response.status_code, 302)

    def test_provinces(self):
        results = self.get("provinces").json()["results"]
        self.assertEqual(len(results), 77)
        bangkok = next(r for r in results if r["code"] == "10")
        self.assertEqual(
            bangkok,
            {"code": "10", "iso": "TH-10", "th": "กรุงเทพมหานคร", "en": "Bangkok"},
        )

    def test_districts_filtered_by_province(self):
        results = self.get("districts", province="10").json()["results"]
        self.assertEqual(len(results), 50)
        self.assertTrue(all(r["code"].startswith("10") for r in results))
        self.assertIn("1001", {r["code"] for r in results})

    def test_subdistricts_filtered_by_district(self):
        results = self.get("subdistricts", district="1001").json()["results"]
        self.assertTrue(results)
        self.assertTrue(all(r["code"].startswith("1001") for r in results))
        first = next(r for r in results if r["code"] == "100101")
        self.assertEqual(first["postcode"], "10200")
        self.assertEqual(first["postcodes"], ["10200"])

    def test_bad_parameters_rejected(self):
        self.assertEqual(self.get("districts").status_code, 400)
        self.assertEqual(self.get("districts", province="1x").status_code, 400)
        self.assertEqual(self.get("subdistricts", district="10").status_code, 400)
        self.assertEqual(self.get("postcode-search", postcode="102").status_code, 400)

    def test_unknown_code_empty(self):
        self.assertEqual(self.get("districts", province="99").json()["results"], [])

    def test_postcode_search_includes_secondary_postcodes(self):
        results = self.get("postcode-search", postcode="10230").json()["results"]
        codes = {r["code"] for r in results}
        self.assertIn("100508", codes)  # Tha Raeng: secondary 10230
        tha_raeng = next(r for r in results if r["code"] == "100508")
        self.assertEqual(tha_raeng["district"]["code"], "1005")
        self.assertEqual(tha_raeng["province"]["iso"], "TH-10")
        self.assertTrue(all("10230" in r["postcodes"] for r in results))

    def test_cached_and_invalidated_by_loader(self):
        def english_names():
            return {
                r["en"] for r in self.get("districts", province="10").json()["results"]
            }

        self.assertIn("Phra Nakhon", english_names())
        District.objects.filter(code="1001").update(name_en="Stale")
        self.assertIn("Phra Nakhon", english_names())  # served from cache
        bump_cache_version()
        self.assertIn("Stale", english_names())
        load_th_geo(Province, District, Subdistrict)  # restores and bumps
        self.assertIn("Phra Nakhon", english_names())


class LookupTests(TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_name("  เขต พระนคร "), "พระนคร")
        self.assertEqual(normalize_name("กรุงเทพฯ"), "กรุงเทพมหานคร")
        self.assertEqual(normalize_name("Phra Nakhon District"), "phra nakhon")

    def test_find_by_thai_or_english(self):
        sub = Subdistrict.objects.get(code="100101")
        self.assertEqual(
            find_subdistrict("กรุงเทพมหานคร", "พระนคร", "พระบรมมหาราชวัง"), sub
        )
        self.assertEqual(
            find_subdistrict("bangkok", "Phra Nakhon", "Phra Borom Maha Ratchawang"),
            sub,
        )
        self.assertIsNone(find_subdistrict("เชียงใหม่", "พระนคร", "พระบรมมหาราชวัง"))
        self.assertIsNone(find_subdistrict("", "พระนคร", "พระบรมมหาราชวัง"))


class ThaiProfileAddressFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        company = make_company("Geo Form Co")
        cls.employee = make_employee(
            company=company, email="f@geo.test", phone="9400000002"
        )

    def form(self, **data):
        return EmployeeThaiProfileForm(
            data, instance=EmployeeThaiProfile(employee_id=self.employee)
        )

    def test_picked_subdistrict_saves_names_and_postcode(self):
        form = self.form(
            th_subdistrict_code="100101", province="x", district="y", subdistrict="z"
        )
        self.assertTrue(form.is_valid(), form.errors)
        profile = form.save()
        self.assertEqual(profile.subdistrict, "พระบรมมหาราชวัง")
        self.assertEqual(profile.district, "พระนคร")
        self.assertEqual(profile.province, "กรุงเทพมหานคร")
        self.assertEqual(profile.postcode, "10200")

    def test_picked_subdistrict_secondary_postcode_accepted(self):
        form = self.form(th_subdistrict_code="100508", postcode="10230")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().postcode, "10230")

    def test_picked_subdistrict_postcode_mismatch_rejected(self):
        form = self.form(th_subdistrict_code="100101", postcode="50200")
        self.assertFalse(form.is_valid())
        self.assertIn("postcode", form.errors)

    def test_typed_known_names_postcode_mismatch_rejected(self):
        form = self.form(
            province="กรุงเทพมหานคร",
            district="พระนคร",
            subdistrict="พระบรมมหาราชวัง",
            postcode="50200",
        )
        self.assertFalse(form.is_valid())
        self.assertIn("postcode", form.errors)

    def test_typed_known_names_fill_postcode(self):
        form = self.form(
            province="Bangkok",
            district="Phra Nakhon",
            subdistrict="Phra Borom Maha Ratchawang",
        )
        self.assertTrue(form.is_valid(), form.errors)
        profile = form.save()
        self.assertEqual(profile.postcode, "10200")
        self.assertEqual(profile.province, "Bangkok")  # typed text kept

    def test_free_text_kept_when_no_match(self):
        form = self.form(
            province="Somewhere",
            district="New district",
            subdistrict="New area",
            postcode="12345",
        )
        self.assertTrue(form.is_valid(), form.errors)
        profile = form.save()
        self.assertEqual(
            (profile.province, profile.district, profile.subdistrict, profile.postcode),
            ("Somewhere", "New district", "New area", "12345"),
        )

    def test_unknown_picked_code_rejected(self):
        form = self.form(th_subdistrict_code="999999")
        self.assertFalse(form.is_valid())
        form = self.form(th_subdistrict_code="abc")
        self.assertFalse(form.is_valid())


class ThaiProfileEditViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        company = make_company("Geo View Co")
        cls.hr_user = make_user("geo_hr")
        make_employee(
            company=company, email="hr@geo.test", phone="9400000003", user=cls.hr_user
        )
        for codename in ("view_employee", "change_employee"):
            cls.hr_user.user_permissions.add(Permission.objects.get(codename=codename))
        cls.target = make_employee(
            company=company, email="t@geo.test", phone="9400000004"
        )

    def test_form_renders_picker_and_saves(self):
        self.client.force_login(self.hr_user)
        url = reverse("employee-thai-profile-edit", args=[self.target.pk])
        page = self.client.get(url, **HX).content.decode()
        self.assertIn(f'id="thGeoPicker{self.target.pk}"', page)
        self.assertIn(reverse("orvahr_geo:districts"), page)
        self.assertIn('name="th_subdistrict_code"', page)
        response = self.client.post(url, {"th_subdistrict_code": "100508"}, **HX)
        self.assertEqual(response.status_code, 200)
        profile = EmployeeThaiProfile._base_manager.get(employee_id=self.target)
        self.assertEqual(profile.subdistrict, "ท่าแร้ง")
        self.assertEqual(profile.postcode, "10220")
