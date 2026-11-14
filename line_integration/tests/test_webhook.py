from unittest import mock

from django.test import Client

from base.models import IntegrationApps
from line_integration import webhook
from line_integration.models import LineUserLink, LineWebhookEvent
from line_integration.tests.helpers import LineTestCase, sign


def text_event(text, user="Ufakeuser0001", event_id="01FAKEEVENT0001", reply="rtok-1"):
    return {
        "destination": "Ufakebot",
        "events": [
            {
                "type": "message",
                "webhookEventId": event_id,
                "deliveryContext": {"isRedelivery": False},
                "replyToken": reply,
                "source": {"type": "user", "userId": user},
                "message": {"type": "text", "id": "1", "text": text},
            }
        ],
    }


class WebhookSignatureTests(LineTestCase):
    def test_missing_signature_rejected(self):
        with mock.patch("line_integration.webhook.handle_event") as handle:
            r = self.webhook_post(text_event("hi"), signature=None)
        self.assertEqual(r.status_code, 403)
        self.assertFalse(handle.called)

    def test_wrong_signature_rejected(self):
        body = '{"events": []}'
        with mock.patch("line_integration.webhook.handle_event") as handle:
            r = self.webhook_post(body, signature=sign(body, "other-secret"))
        self.assertEqual(r.status_code, 403)
        self.assertFalse(handle.called)

    def test_tampered_body_rejected(self):
        import json

        body = json.dumps(text_event("hi"))
        sig = sign(body)
        r = self.webhook_post(body.replace("Ufakeuser0001", "Uattacker0001"), signature=sig)
        self.assertEqual(r.status_code, 403)

    def test_garbage_signature_rejected(self):
        self.assertEqual(self.webhook_post('{"events": []}', signature="!!notbase64").status_code, 403)

    def test_valid_signature_reaches_event_handling(self):
        with mock.patch("line_integration.webhook.handle_event") as handle:
            r = self.webhook_post(text_event("hi"))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(handle.called, "signed payload never reached event handling")

    def test_console_verify_empty_events_ok(self):
        self.assertEqual(self.webhook_post({"destination": "U", "events": []}).status_code, 200)

    def test_signature_checked_before_json_parse(self):
        # invalid JSON with a bad signature is a 403, not a 400
        self.assertEqual(self.webhook_post("{not json", signature="x").status_code, 403)
        self.assertEqual(self.webhook_post("{not json").status_code, 400)

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get(self.channel.webhook_path()).status_code, 405)

    def test_unknown_channel_403(self):
        url = "/line/webhook/00000000-0000-4000-8000-000000000000/"
        self.assertEqual(self.webhook_post({"events": []}, url=url).status_code, 403)

    def test_inactive_channel_403(self):
        self.channel.is_active = False
        self.channel.save()
        self.assertEqual(self.webhook_post({"events": []}).status_code, 403)

    def test_integration_disabled_403(self):
        IntegrationApps.objects.filter(app_label="line_integration").update(is_enabled=False)
        self.assertEqual(self.webhook_post({"events": []}).status_code, 403)

    def test_channel_secret_unreadable_fails_closed(self):
        self.channel.channel_secret_encrypted = "garbage"
        self.channel.save()
        self.assertEqual(self.webhook_post({"events": []}).status_code, 403)

    def test_body_too_large_rejected(self):
        big = '{"events": [], "pad": "' + "x" * (webhook.MAX_BODY_BYTES + 10) + '"}'
        with mock.patch("line_integration.webhook.handle_event") as handle:
            r = self.webhook_post(big)
        self.assertEqual(r.status_code, 413)
        self.assertFalse(handle.called)

    def test_csrf_exempt(self):
        r = self.webhook_post({"events": []}, client=Client(enforce_csrf_checks=True))
        self.assertEqual(r.status_code, 200)

    def test_redelivery_processed_once(self):
        payload = text_event("hello", event_id="01DUPLICATE")
        self.webhook_post(payload)
        payload["events"][0]["deliveryContext"]["isRedelivery"] = True
        self.webhook_post(payload)
        self.assertEqual(LineWebhookEvent.objects.filter(webhook_event_id="01DUPLICATE").count(), 1)
        self.assertEqual(self.http.call_count, 1, "redelivered event replied to twice")


class WebhookEventTests(LineTestCase):
    def test_follow_replies_with_instructions(self):
        payload = {
            "events": [
                {
                    "type": "follow",
                    "webhookEventId": "01FOLLOW",
                    "replyToken": "rtok-f",
                    "source": {"type": "user", "userId": "Unew"},
                }
            ]
        }
        self.assertEqual(self.webhook_post(payload).status_code, 200)
        self.assertEqual(self.http.call_count, 1)
        self.assertTrue(self.http.call_args.args[0].endswith("/message/reply"))
        self.assertEqual(self.http.call_args.kwargs["json"]["replyToken"], "rtok-f")

    def test_unfollow_pauses_link_and_follow_resumes(self):
        emp = self.make_emp("unf")
        LineUserLink.objects.create(employee=emp, channel=self.channel, line_user_id="Uunf")
        self.webhook_post({"events": [{"type": "unfollow", "webhookEventId": "e1",
                                        "source": {"type": "user", "userId": "Uunf"}}]})
        self.assertFalse(LineUserLink.objects.get(employee=emp).is_active)
        self.webhook_post({"events": [{"type": "follow", "webhookEventId": "e2", "replyToken": "r",
                                        "source": {"type": "user", "userId": "Uunf"}}]})
        self.assertTrue(LineUserLink.objects.get(employee=emp).is_active)

    def test_group_source_ignored(self):
        payload = text_event("ABCD-EFGH")
        payload["events"][0]["source"] = {"type": "group", "groupId": "C1", "userId": "U1"}
        self.webhook_post(payload)
        self.assertEqual(self.http.call_count, 0)

    def test_link_code_flow_end_to_end(self):
        from line_integration import linking

        emp = self.make_emp("e2e")
        code, _ = linking.generate_code(emp)
        r = self.webhook_post(text_event(f"link {code.lower()}", user="Ue2e"))
        self.assertEqual(r.status_code, 200)
        link = LineUserLink.objects.get(employee=emp)
        self.assertEqual(link.line_user_id, "Ue2e")
        self.assertEqual(link.channel, self.channel)
        self.assertEqual(self.http.call_args.kwargs["json"]["replyToken"], "rtok-1")
