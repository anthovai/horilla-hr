"""
Minimal LINE Messaging API client.

Contract: no method ever raises into the caller. Every outbound call writes a
LineMessageLog row (status / HTTP code / short error) - never the message
body and never the token.
"""

import logging
import uuid

import requests

from line_integration.models import LineMessageLog

logger = logging.getLogger(__name__)

API_BASE = "https://api.line.me/v2/bot"
PUSH_URL = API_BASE + "/message/push"
REPLY_URL = API_BASE + "/message/reply"
TIMEOUT = (3.05, 10)  # connect, read
MAX_TEXT = 5000  # LINE text message limit


def text_message(text):
    text = str(text)
    if len(text) > MAX_TEXT:
        text = text[: MAX_TEXT - 1] + "…"
    return {"type": "text", "text": text}


def flex_message(alt_text, contents):
    return {"type": "flex", "altText": str(alt_text)[:400], "contents": contents}


class LineClient:
    def __init__(self, channel):
        self.channel = channel

    # Public API ----------------------------------------------------------
    def push(self, to, messages, *, employee=None, event=""):
        """Push up to 5 message objects to a userId. Returns True on 2xx."""
        payload = {"to": to, "messages": list(messages)[:5]}
        # Retry key makes a retried push idempotent on LINE's side.
        return self._post(
            PUSH_URL,
            payload,
            kind="push",
            employee=employee,
            event=event,
            extra_headers={"X-Line-Retry-Key": str(uuid.uuid4())},
        )

    def push_text(self, to, text, **kwargs):
        return self.push(to, [text_message(text)], **kwargs)

    def push_flex(self, to, alt_text, contents, **kwargs):
        return self.push(to, [flex_message(alt_text, contents)], **kwargs)

    def reply(self, reply_token, messages, *, employee=None, event=""):
        payload = {"replyToken": reply_token, "messages": list(messages)[:5]}
        return self._post(REPLY_URL, payload, kind="reply", employee=employee, event=event)

    def reply_text(self, reply_token, text, **kwargs):
        return self.reply(reply_token, [text_message(text)], **kwargs)

    # Internals -----------------------------------------------------------
    def _log(self, kind, status, *, employee, event, http_status=None, error="", request_id=""):
        try:
            LineMessageLog.objects.create(
                channel=self.channel if self.channel and self.channel.pk else None,
                employee=employee,
                kind=kind,
                event=event[:64],
                status=status,
                http_status=http_status,
                error=self._scrub(error)[:255],
                line_request_id=(request_id or "")[:64],
            )
        except Exception:  # logging must not break the caller either
            logger.exception("LINE: could not write LineMessageLog")

    def _scrub(self, text):
        text = str(text or "")
        try:
            for secret in (self.channel.access_token, self.channel.channel_secret):
                if secret and secret in text:
                    text = text.replace(secret, "[redacted]")
        except Exception:
            pass
        return text

    def _post(self, url, payload, *, kind, employee, event, extra_headers=None):
        channel_id = getattr(self.channel, "pk", None)
        try:
            token = self.channel.access_token if self.channel else ""
        except Exception:
            token = ""
        if not token or not getattr(self.channel, "is_active", False):
            self._log(kind, "skipped", employee=employee, event=event, error="channel not configured or inactive")
            return False

        headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        }
        headers.update(extra_headers or {})
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=TIMEOUT)
        except Exception as exc:  # network errors, timeouts, anything
            # Class name only: exception text can echo request details.
            logger.warning("LINE %s failed for channel %s: %s", kind, channel_id, type(exc).__name__)
            self._log(kind, "failed", employee=employee, event=event, error=type(exc).__name__)
            return False

        request_id = response.headers.get("x-line-request-id", "")
        if 200 <= response.status_code < 300:
            self._log(
                kind, "sent", employee=employee, event=event,
                http_status=response.status_code, request_id=request_id,
            )
            return True

        detail = ""
        try:
            detail = str((response.json() or {}).get("message", ""))
        except Exception:
            pass
        logger.warning("LINE %s failed for channel %s: HTTP %s", kind, channel_id, response.status_code)
        self._log(
            kind, "failed", employee=employee, event=event,
            http_status=response.status_code,
            error=f"HTTP {response.status_code}: {detail[:200]}",
            request_id=request_id,
        )
        return False
