"""
PDPA consent on the public job application form.

When a candidate privacy notice is published the form shows it with the
candidate consent purposes; required purposes must be ticked, and the
acknowledgement plus every ticked purpose is recorded against the new
Candidate. Without a published notice the form is exactly as before.
"""

from datetime import date

from django.test import Client, TestCase
from django.urls import reverse

from base.models import Department, JobPosition
from horilla.testkit import make_company
from orvahr_pdpa.models import ConsentPurpose, ConsentRecord, PrivacyNotice
from recruitment.models import Candidate, Recruitment


class ApplicationFormConsentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = make_company("Apply Co")
        dept = Department.objects.create(department="Apply Dept")
        dept.company_id.add(cls.company)
        cls.jp = JobPosition.objects.create(job_position="Apply Dev", department_id=dept)
        cls.rec = Recruitment.objects.create(
            title="Apply Hire",
            description="d",
            company_id=cls.company,
            job_position_id=cls.jp,
            vacancy=1,
            is_published=True,
            optional_resume=True,
            optional_profile_image=True,
        )
        cls.rec.open_positions.add(cls.jp)

    def url(self):
        return f"{reverse('application-form')}?recruitmentId={self.rec.id}"

    def payload(self, email="applicant@example.com", **extra):
        data = {
            "name": "Applicant Person",
            "email": email,
            "mobile": "0812345678",
            "gender": "male",
            "recruitment_id": self.rec.id,
            "job_position_id": self.jp.id,
            "referral_source": "job_board",
            "country": "Thailand",
            "state": "Bangkok",
            "city": "Bangkok",
            "zip": "10110",
        }
        data.update(extra)
        return data

    def publish(self):
        notice = PrivacyNotice.all_objects.create(
            audience="candidate",
            version="1",
            title="Candidate notice",
            text_th="ประกาศความเป็นส่วนตัว",
            effective_from=date(2020, 1, 1),
            published=True,
        )
        required = ConsentPurpose.all_objects.create(
            audience="candidate", code="background", name="Background check", optional=False
        )
        optional = ConsentPurpose.all_objects.create(
            audience="candidate", code="talent-pool", name="Talent pool", optional=True
        )
        return notice, required, optional

    def test_no_notice_form_unchanged(self):
        response = Client().get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["pdpa"])
        self.assertNotContains(response, "pdpa-candidate-consent")

        response = Client().post(self.url(), self.payload())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Candidate.objects.filter(email="applicant@example.com").exists())
        self.assertFalse(ConsentRecord.all_objects.exists())

    def test_notice_and_purposes_are_shown(self):
        notice, required, optional = self.publish()
        response = Client().get(self.url())
        self.assertContains(response, "pdpa-candidate-consent")
        self.assertContains(response, notice.title)
        self.assertContains(response, f'name="pdpa_purposes" value="{required.pk}"')
        self.assertContains(response, f'name="pdpa_purposes" value="{optional.pk}"')

    def test_missing_required_consent_blocks_submission(self):
        _notice, _required, optional = self.publish()
        response = Client().post(self.url(), self.payload(pdpa_purposes=[optional.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Candidate.objects.filter(email="applicant@example.com").exists())
        self.assertFalse(ConsentRecord.all_objects.exists())
        # The ticked box stays ticked on the re-rendered form.
        self.assertEqual(response.context["pdpa_selected"], [str(optional.pk)])
        self.assertTrue(
            any("Background check" in str(m) for m in response.context["messages"])
        )

    def test_required_consent_records_acknowledgement_and_purposes(self):
        notice, required, optional = self.publish()
        response = Client().post(
            self.url(), self.payload(pdpa_purposes=[required.pk, optional.pk])
        )
        self.assertEqual(response.status_code, 200)
        candidate = Candidate.objects.get(email="applicant@example.com")
        records = ConsentRecord.all_objects.filter(candidate=candidate)
        self.assertEqual(records.count(), 3)
        self.assertTrue(
            records.filter(purpose__isnull=True, action="acknowledged", notice=notice).exists()
        )
        self.assertEqual(
            set(records.filter(action="granted").values_list("purpose_id", flat=True)),
            {required.pk, optional.pk},
        )
        self.assertTrue(all(r.channel == "candidate_apply" for r in records))

    def test_optional_purpose_may_be_left_unticked(self):
        _notice, required, optional = self.publish()
        Client().post(self.url(), self.payload(pdpa_purposes=[required.pk]))
        candidate = Candidate.objects.get(email="applicant@example.com")
        granted = ConsentRecord.all_objects.filter(candidate=candidate, action="granted")
        self.assertEqual(list(granted.values_list("purpose_id", flat=True)), [required.pk])
