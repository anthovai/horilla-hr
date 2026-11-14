from datetime import date, timedelta

from django.db import IntegrityError, transaction
from django.urls import reverse

from horilla.testkit.factories import make_candidate, make_recruitment
from orvahr_pdpa import consent
from orvahr_pdpa.models import ConsentPurpose, ConsentRecord, PrivacyNotice
from orvahr_pdpa.tests.helpers import PdpaTestCase


class ConsentTests(PdpaTestCase):
    def setUp(self):
        super().setUp()
        self.emp = self.make_emp("worker")
        self.notice = PrivacyNotice.objects.create(
            audience="employee", version="1.0", title="Employee notice", text_th="ประกาศ",
            effective_from=date.today() - timedelta(days=1), published=True,
        )
        self.photos = ConsentPurpose.objects.create(audience="employee", code="photos", name="Marketing photos")
        self.required = ConsentPurpose.objects.create(audience="employee", code="payroll", name="Payroll", optional=False)

    def test_current_notice_prefers_company_and_published(self):
        PrivacyNotice.objects.create(audience="employee", version="2.0", title="Draft", text_th="x",
                                     effective_from=date.today(), published=False)
        future = PrivacyNotice.objects.create(audience="employee", version="3.0", title="Future", text_th="x",
                                              effective_from=date.today() + timedelta(days=5), published=True)
        own = PrivacyNotice.objects.create(company=self.company, audience="employee", version="1.1", title="Own",
                                           text_th="x", effective_from=date.today() - timedelta(days=30), published=True)
        current = consent.current_notice_for_employee(self.emp)
        self.assertEqual(current, own)
        self.assertNotEqual(current, future)

    def test_acknowledge_once(self):
        self.assertTrue(consent.needs_acknowledgement(self.emp))
        consent.acknowledge_notice(self.emp, self.notice)
        consent.acknowledge_notice(self.emp, self.notice)
        self.assertEqual(ConsentRecord.all_objects.filter(employee=self.emp, action="acknowledged").count(), 1)
        self.assertFalse(consent.needs_acknowledgement(self.emp))

    def test_grant_withdraw_is_append_only(self):
        consent.set_employee_consent(self.emp, self.photos, True)
        consent.set_employee_consent(self.emp, self.photos, True)  # no-op
        consent.set_employee_consent(self.emp, self.photos, False)
        rows = list(ConsentRecord.all_objects.filter(employee=self.emp, purpose=self.photos).order_by("id"))
        self.assertEqual([r.action for r in rows], ["granted", "withdrawn"])
        self.assertEqual(rows[0].notice_version, "1.0")
        self.assertEqual(consent.employee_states(self.emp)[self.photos.pk].action, "withdrawn")

    def test_required_purpose_cannot_be_withdrawn(self):
        with self.assertRaises(ValueError):
            consent.set_employee_consent(self.emp, self.required, False)

    def test_record_needs_exactly_one_subject(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            ConsentRecord.all_objects.create(action="granted")

    def test_self_service_page_flow(self):
        client = self.client_for(self.emp)
        url = reverse("orvahr_pdpa:my-privacy")
        r = client.get(url)
        self.assertContains(r, "ประกาศ")
        client.post(url, {"action": "acknowledge"})
        client.post(url, {"action": "grant", "purpose": self.photos.pk})
        client.post(url, {"action": "withdraw", "purpose": self.required.pk})  # refused
        states = consent.employee_states(self.emp)
        self.assertEqual(states[None].action, "acknowledged")
        self.assertEqual(states[self.photos.pk].action, "granted")
        self.assertNotIn(self.required.pk, states)
        record = states[self.photos.pk]
        self.assertEqual(record.channel, "web")
        self.assertEqual(record.recorded_by, self.emp.employee_user_id)
        self.assertEqual(record.ip_address, "127.0.0.1")

    def test_hr_consent_status(self):
        officer = self.make_officer()
        consent.acknowledge_notice(self.emp, self.notice)
        consent.set_employee_consent(self.emp, self.photos, True)
        r = self.client_for(officer).get(reverse("orvahr_pdpa:consent-status"))
        self.assertEqual(r.status_code, 200)
        row = next(row for row in r.context["rows"] if row["employee"].pk == self.emp.pk)
        self.assertTrue(row["acknowledged"])
        self.assertIn("granted", [rec.action for rec in row["purposes"] if rec])
        plain = self.make_emp("nosy")
        r = self.client_for(plain).get(reverse("orvahr_pdpa:consent-status"))
        self.assertNotContains(r, "pdpa-consents", status_code=r.status_code)

    def test_candidate_consent(self):
        rec = make_recruitment(company=self.company)
        candidate = make_candidate(recruitment=rec, email="cand@test.horilla")
        PrivacyNotice.objects.create(audience="candidate", version="1", title="Candidate notice", text_th="x",
                                     effective_from=date.today(), published=True)
        talent = ConsentPurpose.objects.create(audience="candidate", code="talent-pool", name="Talent pool")
        records = consent.record_candidate_consent(candidate, [talent.pk, self.photos.pk])
        self.assertEqual([r.action for r in records], ["acknowledged", "granted"])
        self.assertTrue(all(r.candidate_id == candidate.pk and r.employee_id is None for r in records))
