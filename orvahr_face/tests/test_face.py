"""Face check-in: consent, enrolment, verification, punch, deletion, legacy retirement."""

import os
import shutil
import tempfile
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from attendance.models import AttendanceActivity
from orvahr_face import consent, services
from orvahr_face.client import FaceServiceClient
from orvahr_face.crypto import decrypt_template, encrypt_template
from orvahr_face.hooks import web_punch_denial
from orvahr_face.models import FaceAttempt, FaceTemplate, activity_method
from orvahr_face.tests.helpers import (
    FAKE_JPEG,
    MODEL,
    VECTOR,
    FaceTestCase,
    embed_rejected,
    unavailable,
    verify_result,
)
from orvahr_pdpa.models import ConsentPurpose, ConsentRecord, SensitiveAccessLog


class ConsentPurposeTests(FaceTestCase):
    def test_purpose_seeded_optional_with_thai_and_alternative(self):
        purpose = ConsentPurpose.all_objects.get(company__isnull=True, code=consent.PURPOSE_CODE)
        self.assertTrue(purpose.optional)
        self.assertEqual(purpose.audience, "employee")
        self.assertIn("มาตรา 26", purpose.description)
        self.assertIn("Alternative", purpose.description)

    def test_enrol_refused_without_consent(self):
        response = self.enrol()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "no_consent")
        self.service.embed.assert_not_called()
        self.assertFalse(FaceTemplate.all_objects.exists())

    def test_consent_page_records_explicit_grant_only_when_ticked(self):
        client = self.client_for(self.emp)
        client.post("/face/", {"action": "grant"})
        self.assertFalse(consent.has_consent(self.emp))
        client.post("/face/", {"action": "grant", "agree": "yes"})
        self.assertTrue(consent.has_consent(self.emp))

    def test_disabled_company_refuses(self):
        self.settings_row.enabled = False
        self.settings_row.save()
        self.give_consent()
        self.assertEqual(self.enrol().json()["code"], "disabled")


class EnrolmentTests(FaceTestCase):
    def setUp(self):
        super().setUp()
        self.give_consent()

    def test_success_stores_encrypted_template_and_logs(self):
        response = self.enrol()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["ok"])
        template = FaceTemplate.all_objects.get(employee=self.emp)
        self.assertEqual(template.full_model, MODEL)
        self.assertEqual(template.frames_count, 5)
        self.assertEqual(template.source, "self")
        # The service got the frames without the data-URL prefix.
        sent = self.service.embed.call_args[0][0]
        self.assertEqual(len(sent), 5)
        self.assertFalse(sent[0].startswith("data:"))
        # Encrypted at rest: the raw column is not the float list.
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT template_encrypted FROM {FaceTemplate._meta.db_table} WHERE id = %s", [template.pk]
            )
            raw = cursor.fetchone()[0]
        self.assertNotIn("[", raw)
        self.assertNotIn(repr(VECTOR[1])[:8], raw)
        self.assertEqual(decrypt_template(raw, self.emp.pk), VECTOR)
        self.assertNotIn(raw, repr(template))
        log = SensitiveAccessLog.all_objects.get(subject_employee=self.emp, context__startswith="face_enrol")
        self.assertEqual(log.fields, ["face_template"])
        self.assertTrue(FaceAttempt.all_objects.filter(employee=self.emp, kind="enrol", outcome="success").exists())

    def test_at_most_three_templates(self):
        for _ in range(4):
            self.enrol()
        self.assertEqual(FaceTemplate.all_objects.filter(employee=self.emp).count(), 3)

    def test_rejected_frames_return_reasons(self):
        self.service.embed.return_value = embed_rejected(["no_face", "blurry", "multiple_faces"])
        response = self.client_for(self.emp).post(
            "/face/api/enrol/", {"images": [FAKE_JPEG] * 3}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 422)
        frames = response.json()["frames"]
        self.assertEqual([f["reason"] for f in frames], ["no_face", "blurry", "multiple_faces"])
        self.assertTrue(all(f["message"] and not f["accepted"] for f in frames))
        self.assertFalse(FaceTemplate.all_objects.exists())

    def test_bad_image_refused_before_service(self):
        response = self.client_for(self.emp).post(
            "/face/api/enrol/", {"images": ["data:image/gif;base64,R0lGODlh"]}, content_type="application/json"
        )
        self.assertEqual(response.json()["code"], "invalid_image")
        too_many = self.client_for(self.emp).post(
            "/face/api/enrol/", {"images": [FAKE_JPEG] * 6}, content_type="application/json"
        )
        self.assertEqual(too_many.json()["code"], "invalid_image")
        self.service.embed.assert_not_called()

    def test_service_unavailable_is_graceful(self):
        self.service.embed.return_value = unavailable()
        response = self.client_for(self.emp).post(
            "/face/api/enrol/", {"images": [FAKE_JPEG]}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "service_unavailable")
        self.assertTrue(response.json()["message"])

    def test_pages_render(self):
        client = self.client_for(self.emp)
        for url in ("/face/", "/face/enrol/", "/face/check-in/"):
            self.assertEqual(client.get(url).status_code, 200, url)


class NoImagePersistedTests(FaceTestCase):
    def test_media_root_and_temp_dir_unchanged(self):
        media = tempfile.mkdtemp()
        temp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, True)
        self.addCleanup(shutil.rmtree, temp, True)
        old_tempdir = tempfile.tempdir
        tempfile.tempdir = temp
        try:
            with override_settings(MEDIA_ROOT=media, FILE_UPLOAD_TEMP_DIR=temp):
                self.give_consent()
                self.assertEqual(self.enrol().status_code, 200)
                self.service.verify.return_value = verify_result()
                self.assertEqual(self.check("in").status_code, 200)
        finally:
            tempfile.tempdir = old_tempdir
        self.assertEqual(list(os.walk(media)), [(media, [], [])])
        self.assertEqual(os.listdir(temp), [])


class VerifyTests(FaceTestCase):
    def setUp(self):
        super().setUp()
        self.give_consent()
        self.enrol()

    def test_match_creates_attendance_with_method_face(self):
        self.service.verify.return_value = verify_result()
        response = self.check("in")
        self.assertEqual(response.status_code, 200, response.content)
        activity = AttendanceActivity._base_manager.get(employee_id=self.emp)
        self.assertEqual(activity_method(activity), "face")
        _image, templates, model = self.service.verify.call_args[0]
        self.assertEqual(templates, [VECTOR])
        self.assertEqual(model, MODEL)
        log = SensitiveAccessLog.all_objects.filter(subject_employee=self.emp, context="face_verify").first()
        self.assertIsNotNone(log)
        # Check-out goes through the same path.
        response = self.check("out")
        self.assertEqual(response.status_code, 200, response.content)
        activity.refresh_from_db()
        self.assertIsNotNone(activity.clock_out)
        self.assertEqual(
            FaceAttempt.all_objects.filter(attendance_activity=activity, outcome="success").count(), 2
        )

    def test_wrong_direction_refused(self):
        self.service.verify.return_value = verify_result()
        self.assertEqual(self.check("out").json()["code"], "state_changed")
        self.service.verify.assert_not_called()

    def test_liveness_failure_refused(self):
        self.service.verify.return_value = verify_result(match=False, live=False, reason="not_live")
        response = self.check("in")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "not_live")
        self.assertFalse(AttendanceActivity._base_manager.filter(employee_id=self.emp).exists())

    def test_threshold_override(self):
        self.settings_row.match_threshold = 0.9
        self.settings_row.save()
        self.service.verify.return_value = verify_result(score=0.85)
        self.assertEqual(self.check("in").json()["code"], "no_match")

    def test_rate_limit(self):
        self.service.verify.return_value = verify_result(match=False, score=0.2)
        for _ in range(5):
            self.assertEqual(self.check("in").json()["code"], "no_match")
        response = self.check("in")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["code"], "rate_limited")
        self.assertEqual(self.service.verify.call_count, 5)
        self.assertEqual(
            FaceAttempt.all_objects.filter(employee=self.emp, kind="verify").exclude(outcome="success").count(), 6
        )

    def test_service_down(self):
        self.service.verify.return_value = unavailable()
        response = self.check("in")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "service_unavailable")

    def test_attendance_rules_still_apply(self):
        from base.models import AttendanceAllowedIP

        AttendanceAllowedIP.objects.create(
            company_id=None, is_enabled=True, additional_data={"allowed_ips": ["10.9.9.0/24"]}
        )
        AttendanceAllowedIP.objects.create(
            company_id=self.company, is_enabled=True, additional_data={"allowed_ips": ["10.9.9.0/24"]}
        )
        self.service.verify.return_value = verify_result()
        response = self.check("in")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "attendance_refused")
        self.assertFalse(AttendanceActivity._base_manager.filter(employee_id=self.emp).exists())

    def test_required_for_web_blocks_plain_button_only_for_enrolled(self):
        self.settings_row.required_for_web = True
        self.settings_row.save()
        request = mock.Mock(user=self.emp.employee_user_id, orvahr_face_verified=False)
        self.assertIsNotNone(web_punch_denial(request, self.company))
        response = self.client_for(self.emp).get("/attendance/clock-in/", HTTP_HX_REQUEST="true")
        self.assertFalse(AttendanceActivity._base_manager.filter(employee_id=self.emp).exists())
        self.assertNotEqual(response.status_code, 500)
        # Not consenting is always allowed to use the normal button.
        other = self.make_emp("napat")
        self.assertIsNone(web_punch_denial(mock.Mock(user=other.employee_user_id, orvahr_face_verified=False)))
        self.client_for(other).get("/attendance/clock-in/", HTTP_HX_REQUEST="true")
        self.assertTrue(AttendanceActivity._base_manager.filter(employee_id=other).exists())


class DeletionTests(FaceTestCase):
    def setUp(self):
        super().setUp()
        self.give_consent()
        self.enrol()
        self.assertTrue(FaceTemplate.all_objects.filter(employee=self.emp).exists())

    def _deleted_logged(self, reason):
        self.assertTrue(
            FaceAttempt.all_objects.filter(employee=self.emp, kind="delete", detail__startswith=reason).exists()
        )

    def test_withdrawal_on_face_page_deletes_templates(self):
        self.client_for(self.emp).post("/face/", {"action": "withdraw"})
        self.assertFalse(consent.has_consent(self.emp))
        self.assertFalse(FaceTemplate.all_objects.filter(employee=self.emp).exists())
        self._deleted_logged("consent_withdrawn")
        self.assertTrue(ConsentRecord.all_objects.filter(employee=self.emp, action="withdrawn").exists())

    def test_withdrawal_on_my_privacy_deletes_templates(self):
        purpose = consent.purpose_for(self.emp)
        self.client_for(self.emp).post("/pdpa/my-privacy/", {"action": "withdraw", "purpose": purpose.pk})
        self.assertFalse(FaceTemplate.all_objects.filter(employee=self.emp).exists())

    def test_archive_deletes_templates(self):
        self.emp.is_active = False
        self.emp.save()
        self.assertFalse(FaceTemplate.all_objects.filter(employee=self.emp).exists())
        self._deleted_logged("archived")
        log = SensitiveAccessLog.all_objects.filter(subject_employee=self.emp, context="face_delete:archived")
        self.assertTrue(log.exists())

    def test_retention_job(self):
        FaceTemplate.all_objects.filter(employee=self.emp).update(created_at=timezone.now() - timedelta(days=800))
        self.assertFalse(services.is_enrolled(self.emp))
        self.assertEqual(services.purge_expired_templates(), 1)
        self.assertFalse(FaceTemplate.all_objects.exists())

    def test_template_bound_to_employee(self):
        other = self.make_emp("other")
        token = FaceTemplate.all_objects.get(employee=self.emp).template_encrypted
        self.assertIsNone(decrypt_template(token, other.pk))
        self.assertEqual(decrypt_template(encrypt_template([1.0, 2.0], other.pk), other.pk), [1.0, 2.0])


class PermissionTests(FaceTestCase):
    def test_employee_cannot_use_hr_pages(self):
        client = self.client_for(self.emp)
        self.assertEqual(client.get("/face/enrolments/").status_code, 302)
        self.assertEqual(client.get("/face/settings/").status_code, 302)
        self.assertEqual(client.get(f"/face/enrol/{self.emp.pk}/").status_code, 403)
        other = self.make_emp("other")
        consent.grant(other)
        response = client.post(
            "/face/api/enrol/", {"employee_id": other.pk, "images": [FAKE_JPEG]}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 403)
        response = client.post(f"/face/enrolments/{other.pk}/delete/")
        self.assertEqual(response.status_code, 302)
        self.service.embed.assert_not_called()

    def test_hr_assisted_enrolment_and_list(self):
        hr = self.make_emp("hr", is_superuser=True)
        client = self.client_for(hr)
        self.give_consent()
        response = self.enrol(emp=None, client=None)
        self.assertEqual(response.status_code, 200)
        response = client.post(
            "/face/api/enrol/", {"employee_id": self.emp.pk, "images": [FAKE_JPEG]}, content_type="application/json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(FaceTemplate.all_objects.filter(employee=self.emp, source="hr").count(), 1)
        page = client.get("/face/enrolments/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, str(self.emp))
        self.assertNotContains(page, FaceTemplate.all_objects.first().template_encrypted[:20])
        self.assertEqual(client.get("/face/settings/").status_code, 200)
        client.post(f"/face/enrolments/{self.emp.pk}/delete/")
        self.assertFalse(FaceTemplate.all_objects.filter(employee=self.emp).exists())

    def test_settings_saved_for_selected_company(self):
        hr = self.make_emp("hr2", is_superuser=True)
        client = self.client_for(hr)
        session = client.session
        session["selected_company"] = str(self.company.pk)
        session.save()
        client.post(
            "/face/settings/",
            {"enabled": "on", "liveness_required": "on", "match_threshold": "0.7", "retention_days": "365"},
        )
        self.settings_row.refresh_from_db()
        self.assertEqual(self.settings_row.match_threshold, 0.7)
        self.assertEqual(self.settings_row.retention_days, 365)


class LegacyFaceDetectionTests(FaceTestCase):
    def api_client(self):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(self.emp.employee_user_id)
        return client

    def test_upload_refused_when_face_enabled(self):
        from facedetection.models import FaceDetection

        FaceDetection.objects.create(company_id=self.company, start=True)
        response = self.api_client().post("/api/facedetection/setup/", {})
        self.assertEqual(response.status_code, 410)

    def test_flag_off_company_without_face_keeps_legacy(self):
        from facedetection.models import FaceDetection
        from horilla.tenancy import is_multi_tenant

        self.settings_row.enabled = False
        self.settings_row.save()
        FaceDetection.objects.create(company_id=self.company, start=True)
        response = self.api_client().post("/api/facedetection/setup/", {})
        if is_multi_tenant():
            self.assertEqual(response.status_code, 410)
        else:
            # Reaches the legacy serializer (no image sent -> 400), not refused.
            self.assertEqual(response.status_code, 400)

    def test_purge_command_counts_only(self):
        from django.core.files.base import ContentFile

        from facedetection.models import EmployeeFaceDetection

        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, True)
        with override_settings(MEDIA_ROOT=media):
            row = EmployeeFaceDetection(employee_id=self.emp)
            row.image.save("legacy.jpg", ContentFile(b"\xff\xd8\xff" + b"0" * 100), save=True)
            path = row.image.path
            self.assertTrue(os.path.exists(path))
            out = StringIO()
            call_command("purge_legacy_face_photos", "--dry-run", stdout=out)
            self.assertIn("Would delete 1 photo record(s), 1 file(s)", out.getvalue())
            self.assertTrue(EmployeeFaceDetection.objects.exists())
            out = StringIO()
            call_command("purge_legacy_face_photos", stdout=out)
            self.assertIn("Deleted 1 photo record(s), 1 file(s)", out.getvalue())
            self.assertNotIn(str(self.emp), out.getvalue())
            self.assertFalse(EmployeeFaceDetection.objects.exists())
            self.assertFalse(os.path.exists(path))


class ClientTests(FaceTestCase):
    def test_client_never_raises(self):
        import requests

        client = FaceServiceClient(base_url="http://face.invalid", token="t")
        with mock.patch("orvahr_face.client.requests.post", side_effect=requests.ConnectionError("x")):
            self.assertEqual(client.embed([FAKE_JPEG]).error, "unavailable")
        with mock.patch("orvahr_face.client.requests.post", side_effect=requests.Timeout()):
            self.assertEqual(client.verify("x", [], MODEL).error, "timeout")
        response = mock.Mock(status_code=401)
        response.json.return_value = {}
        with mock.patch("orvahr_face.client.requests.post", return_value=response) as post:
            self.assertEqual(client.embed([]).error, "unauthorized")
            self.assertEqual(post.call_args.kwargs["headers"]["X-Service-Token"], "t")
            self.assertIsNotNone(post.call_args.kwargs["timeout"])
        response = mock.Mock(status_code=200)
        response.json.side_effect = ValueError
        with mock.patch("orvahr_face.client.requests.post", return_value=response):
            self.assertEqual(client.embed([]).error, "bad_response")

    @override_settings(ORVAHR_FACE_SERVICE_URL="http://svc:1/", ORVAHR_FACE_SERVICE_TOKEN="tok")
    def test_settings(self):
        client = FaceServiceClient()
        self.assertEqual(client.base_url, "http://svc:1")
        self.assertEqual(client.token, "tok")
