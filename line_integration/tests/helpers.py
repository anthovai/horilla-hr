"""
Shared fixtures. Every test class patches requests.post in the client module,
so no test can reach api.line.me. Tokens below are obviously fake.
"""

import base64
import hashlib
import hmac
import json
from unittest import mock

from django.test import TestCase, override_settings

from base.models import IntegrationApps
from horilla.testkit import make_company, make_employee, make_user
from line_integration.models import LineChannel

FAKE_TOKEN = "fake-line-access-token-NOT-REAL-0123456789"
FAKE_SECRET = "fakechannelsecretnotreal0000abcd"


def fake_response(status=200, body=None, request_id="req-123"):
    resp = mock.Mock()
    resp.status_code = status
    resp.headers = {"x-line-request-id": request_id}
    resp.json.return_value = body if body is not None else {}
    return resp


def sign(body, secret=FAKE_SECRET):
    if isinstance(body, str):
        body = body.encode()
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


@override_settings(LINE_INTEGRATION_ASYNC=False)
class LineTestCase(TestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch(
            "line_integration.client.requests.post", return_value=fake_response()
        )
        self.http = patcher.start()
        self.addCleanup(patcher.stop)

        IntegrationApps.objects.update_or_create(
            app_label="line_integration", company=None, defaults={"is_enabled": True}
        )
        self.company = make_company("LINE Co")
        self.channel = LineChannel(name="Main OA", company=self.company, basic_id="@fake123")
        self.channel.access_token = FAKE_TOKEN
        self.channel.channel_secret = FAKE_SECRET
        self.channel.public_base_url = "https://hr.example.test"
        self.channel.save()

    def make_emp(self, username, **kwargs):
        user = make_user(username, password="pass", **kwargs)
        return make_employee(
            company=self.company, email=f"{username}@test.horilla", user=user
        )

    def webhook_post(self, payload, signature="__auto__", client=None, url=None):
        body = payload if isinstance(payload, (bytes, str)) else json.dumps(payload)
        headers = {}
        if signature == "__auto__":
            signature = sign(body)
        if signature is not None:
            headers["HTTP_X_LINE_SIGNATURE"] = signature
        return (client or self.client).post(
            url or self.channel.webhook_path(),
            data=body,
            content_type="application/json",
            **headers,
        )

    def sent_payloads(self):
        return [c.kwargs["json"] for c in self.http.call_args_list]
