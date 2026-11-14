from datetime import date
from io import StringIO

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.urls import reverse

from employee.models import Employee, EmployeeThaiProfile, EmployeeWorkInformation
from orvahr_pdpa.forms import RetentionPolicyForm
from orvahr_pdpa.models import RetentionPolicy
from orvahr_pdpa.retention import dry_run_report, expired_queryset, subtract_years
from orvahr_pdpa.tests.helpers import PdpaTestCase, thai_id


class RetentionTests(PdpaTestCase):
    def setUp(self):
        super().setUp()
        self.old = self.make_emp("old", first_name="Oldie")
        self.recent = self.make_emp("recent", first_name="Recent")
        EmployeeWorkInformation.objects.filter(employee_id=self.old).update(contract_end_date=date(2010, 1, 1))
        EmployeeWorkInformation.objects.filter(employee_id=self.recent).update(contract_end_date=date(2025, 1, 1))
        EmployeeThaiProfile.objects.create(employee_id=self.old, national_id=thai_id("110170012345"))
        self.policy = RetentionPolicy.objects.create(
            name="Ex-employees", model_label="employee.Employee",
            date_field="employee_work_info__contract_end_date", keep_years=10, action="anonymise",
        )

    def test_subtract_years_leap_day(self):
        self.assertEqual(subtract_years(date(2024, 2, 29), 1), date(2023, 2, 28))

    def test_expired_selection(self):
        ids = list(expired_queryset(self.policy, today=date(2026, 6, 1)).values_list("pk", flat=True))
        self.assertEqual(ids, [self.old.pk])

    def test_dry_run_never_deletes_or_changes(self):
        count = Employee.objects.entire().count()
        rows = dry_run_report(today=date(2026, 6, 1))
        self.assertEqual(rows[0]["count"], 1)
        out = StringIO()
        call_command("pdpa_retention_report", stdout=out)
        self.assertIn("DRY RUN", out.getvalue())
        self.assertIn(f"id {self.old.pk}", out.getvalue())
        self.assertNotIn("Oldie", out.getvalue())
        self.assertEqual(Employee.objects.entire().count(), count)
        self.old.refresh_from_db()
        self.assertEqual(self.old.employee_first_name, "Oldie")
        self.assertTrue(EmployeeThaiProfile.objects.filter(employee_id=self.old).exists())

    def test_misconfigured_policy_reported_not_raised(self):
        bad = RetentionPolicy.objects.create(name="Bad", model_label="employee.Employee", date_field="nope", keep_years=1)
        rows = {row["policy"].pk: row for row in dry_run_report()}
        self.assertTrue(rows[bad.pk]["error"])

    def test_validation(self):
        form = RetentionPolicyForm(data={
            "name": "x", "model_label": "recruitment.Candidate", "date_field": "email",
            "keep_years": 1, "action": "flag", "is_active": "on",
        })
        self.assertFalse(form.is_valid())
        policy = RetentionPolicy(name="x", model_label="recruitment.Candidate", date_field="created_at",
                                 keep_years=1, action="anonymise")
        with self.assertRaises(ValidationError):
            policy.clean()

    def _anonymise(self, emp, ids, confirm="ANONYMISE"):
        return self.client_for(emp).post(
            reverse("orvahr_pdpa:retention-anonymise", args=[self.policy.pk]),
            {"confirm": confirm, "record_ids": ",".join(str(i) for i in ids)},
        )

    def test_anonymise_requires_superuser(self):
        officer = self.make_officer()
        self._anonymise(officer, [self.old.pk])
        self.old.refresh_from_db()
        self.assertEqual(self.old.employee_first_name, "Oldie")

    def test_anonymise_requires_confirmation(self):
        admin = self.make_emp("root", is_superuser=True)
        self._anonymise(admin, [self.old.pk], confirm="yes")
        self.old.refresh_from_db()
        self.assertEqual(self.old.employee_first_name, "Oldie")

    def test_anonymise_only_expired_records(self):
        admin = self.make_emp("root", is_superuser=True)
        self._anonymise(admin, [self.old.pk, self.recent.pk, admin.pk])
        self.old.refresh_from_db()
        self.recent.refresh_from_db()
        self.assertEqual(self.old.employee_first_name, "Anonymised")
        self.assertTrue(self.old.email.endswith("@anonymised.invalid"))
        self.assertFalse(self.old.is_active)
        self.assertFalse(EmployeeThaiProfile.objects.filter(employee_id=self.old).exists())
        self.old.employee_user_id.refresh_from_db()
        self.assertFalse(self.old.employee_user_id.is_active)
        self.assertFalse(self.old.employee_user_id.has_usable_password())
        self.assertEqual(self.recent.employee_first_name, "Recent")
        # Already anonymised: no longer listed as past retention.
        self.assertFalse(expired_queryset(self.policy).filter(pk=self.old.pk).exists())

    def test_flag_policy_cannot_anonymise(self):
        self.policy.action = "flag"
        self.policy.save()
        admin = self.make_emp("root", is_superuser=True)
        self._anonymise(admin, [self.old.pk])
        self.old.refresh_from_db()
        self.assertEqual(self.old.employee_first_name, "Oldie")
