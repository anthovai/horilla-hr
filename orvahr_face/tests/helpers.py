import base64
from unittest import mock

from django.test import Client, TestCase

from attendance.models import AttendanceGeneralSetting
from horilla.horilla_middlewares import set_selected_company
from horilla.testkit import make_company, make_employee, make_user
from orvahr_face import consent
from orvahr_face.client import REJECTED, UNAVAILABLE, ServiceResult
from orvahr_face.models import FaceSettings

# Not a real picture: JPEG magic bytes plus padding, enough for clean_image().
FAKE_JPEG = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 1200).decode()
VECTOR = [0.0123456789 * (i % 7 - 3) for i in range(128)]
MODEL = "arcface-r50@1.0"


def embed_ok(frames=5):
    return ServiceResult(
        ok=True,
        status=200,
        data={
            "template": list(VECTOR),
            "dims": len(VECTOR),
            "model": MODEL,
            "frames": [
                {"faces": 1, "quality": 0.9, "live_score": 0.95, "accepted": True, "reason": None}
                for _ in range(frames)
            ],
        },
    )


def embed_rejected(reasons):
    return ServiceResult(
        ok=False,
        status=422,
        error=REJECTED,
        data={
            "frames": [
                {"faces": 0 if r == "no_face" else 1, "quality": 0.1, "live_score": 0.1, "accepted": False, "reason": r}
                for r in reasons
            ]
        },
    )


def verify_result(match=True, live=True, score=0.82, faces=1, reason=None, threshold=0.6):
    return ServiceResult(
        ok=True,
        status=200,
        data={
            "match": match,
            "score": score,
            "threshold": threshold,
            "live": live,
            "live_score": 0.9 if live else 0.1,
            "faces": faces,
            "reason": reason,
        },
    )


def unavailable():
    return ServiceResult(ok=False, error=UNAVAILABLE)


class FaceTestCase(TestCase):
    def setUp(self):
        super().setUp()
        set_selected_company(None)
        self.company = make_company("Face Co")
        self.settings_row = FaceSettings.all_objects.create(company=self.company, enabled=True)
        for company in (self.company, None):
            AttendanceGeneralSetting.objects.create(company_id=company, enable_check_in=True)
        self.emp = self.make_emp("somchai")
        self.service = mock.MagicMock()
        patcher = mock.patch("orvahr_face.services.face_client.get_client", return_value=self.service)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        set_selected_company(None)
        super().tearDown()

    def make_emp(self, username, company=None, **kwargs):
        user = make_user(username, password="pass", **kwargs)
        return make_employee(
            company=company or self.company, email=f"{username}@test.horilla", first_name=username.title(), user=user
        )

    def client_for(self, emp_or_user):
        user = getattr(emp_or_user, "employee_user_id", emp_or_user)
        client = Client()
        client.force_login(user)
        return client

    def give_consent(self, emp=None):
        self.assertTrue(consent.grant(emp or self.emp))

    def enrol(self, emp=None, client=None):
        self.service.embed.return_value = embed_ok()
        emp = emp or self.emp
        return (client or self.client_for(emp)).post(
            "/face/api/enrol/", {"images": [FAKE_JPEG] * 5}, content_type="application/json"
        )

    def check(self, direction="in", client=None):
        return (client or self.client_for(self.emp)).post(
            "/face/api/check-in/", {"image": FAKE_JPEG, "direction": direction}, content_type="application/json"
        )
