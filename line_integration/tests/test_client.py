import requests
from django.db import connection

from line_integration.client import PUSH_URL, LineClient
from line_integration.models import LineChannel, LineMessageLog
from line_integration.tests.helpers import (
    FAKE_SECRET,
    FAKE_TOKEN,
    LineTestCase,
    fake_response,
)


class ClientTests(LineTestCase):
    def test_push_success_logged(self):
        ok = LineClient(self.channel).push_text("Uabc", "hello", event="test")
        self.assertTrue(ok)
        url = self.http.call_args.args[0]
        kwargs = self.http.call_args.kwargs
        self.assertEqual(url, PUSH_URL)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + FAKE_TOKEN)
        self.assertIn("X-Line-Retry-Key", kwargs["headers"])
        self.assertIsNotNone(kwargs["timeout"])
        self.assertEqual(kwargs["json"]["messages"][0], {"type": "text", "text": "hello"})
        log = LineMessageLog.objects.get()
        self.assertEqual((log.status, log.http_status, log.line_request_id), ("sent", 200, "req-123"))

    def test_log_has_no_message_body(self):
        LineClient(self.channel).push_text("Uabc", "secret leave details")
        log = LineMessageLog.objects.get()
        self.assertNotIn("secret leave details", str(log.__dict__))

    def test_http_error_logged_without_token(self):
        self.http.return_value = fake_response(
            401, {"message": "Authentication failed " + FAKE_TOKEN}
        )
        with self.assertLogs("line_integration.client", level="WARNING") as logs:
            ok = LineClient(self.channel).push_text("Uabc", "x")
        self.assertFalse(ok)
        log = LineMessageLog.objects.get()
        self.assertEqual((log.status, log.http_status), ("failed", 401))
        self.assertIn("Authentication failed", log.error)
        self.assertNotIn(FAKE_TOKEN, log.error)
        self.assertNotIn(FAKE_TOKEN, "\n".join(logs.output))

    def test_network_error_never_raises(self):
        self.http.side_effect = requests.exceptions.ConnectTimeout(
            "timeout with Bearer " + FAKE_TOKEN
        )
        with self.assertLogs("line_integration.client", level="WARNING") as logs:
            ok = LineClient(self.channel).push_text("Uabc", "x")
        self.assertFalse(ok)
        log = LineMessageLog.objects.get()
        self.assertEqual(log.status, "failed")
        self.assertEqual(log.error, "ConnectTimeout")
        self.assertNotIn(FAKE_TOKEN, "\n".join(logs.output))

    def test_unexpected_exception_never_raises(self):
        self.http.side_effect = RuntimeError("boom")
        self.assertFalse(LineClient(self.channel).push_flex("U", "alt", {"type": "bubble"}))
        self.assertEqual(LineMessageLog.objects.get().status, "failed")

    def test_inactive_channel_skipped(self):
        self.channel.is_active = False
        self.assertFalse(LineClient(self.channel).push_text("U", "x"))
        self.assertFalse(self.http.called)
        self.assertEqual(LineMessageLog.objects.get().status, "skipped")

    def test_long_text_truncated(self):
        LineClient(self.channel).push_text("U", "x" * 6000)
        self.assertEqual(len(self.http.call_args.kwargs["json"]["messages"][0]["text"]), 5000)


class EncryptionAtRestTests(LineTestCase):
    def test_credentials_not_stored_in_plain_text(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT access_token_encrypted, channel_secret_encrypted FROM "
                "line_integration_linechannel WHERE id = %s",
                [self.channel.pk],
            )
            token_col, secret_col = cursor.fetchone()
        self.assertNotIn(FAKE_TOKEN, token_col)
        self.assertNotIn(FAKE_SECRET, secret_col)
        fresh = LineChannel.all_objects.get(pk=self.channel.pk)
        self.assertEqual(fresh.access_token, FAKE_TOKEN)
        self.assertEqual(fresh.channel_secret, FAKE_SECRET)

    def test_mask(self):
        self.assertEqual(self.channel.masked_access_token(), "•" * 8 + FAKE_TOKEN[-4:])
        self.assertNotIn(FAKE_SECRET[:-4], self.channel.masked_channel_secret())
