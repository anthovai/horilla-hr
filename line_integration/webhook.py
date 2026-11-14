"""
LINE webhook endpoint: /line/webhook/<webhook_key>/

Order of checks (nothing is parsed before the signature is verified):
  1. POST only                                  -> 405
  2. known, active, configured channel and
     integration enabled                         -> 403 (no oracle)
  3. body size (Content-Length, then actual)    -> 413
  4. X-Line-Signature = base64(HMAC-SHA256(channel secret, raw body)),
     compared with hmac.compare_digest           -> 403
  5. JSON parse                                 -> 400
  6. events, each deduplicated on webhookEventId -> 200
"""

import base64
import hashlib
import hmac
import json
import logging

from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.utils.translation import gettext as _
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from line_integration import linking
from line_integration.client import LineClient, text_message
from line_integration.dispatch import integration_enabled
from line_integration.models import LineChannel, LineUserLink, LineWebhookEvent

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 256 * 1024  # LINE deliveries are a few KB


def signature_valid(channel_secret, body, header):
    if not channel_secret or not header:
        return False
    expected = base64.b64encode(
        hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    ).decode("ascii")
    return hmac.compare_digest(expected, header.strip())


@csrf_exempt
@require_POST
def line_webhook(request, webhook_key):
    channel = LineChannel.all_objects.filter(webhook_key=webhook_key, is_active=True).first()
    if channel is None or not integration_enabled():
        return HttpResponse(status=403)

    try:
        declared = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        return HttpResponse(status=400)
    if declared > MAX_BODY_BYTES:
        return HttpResponse(status=413)
    body = request.body
    if len(body) > MAX_BODY_BYTES:
        return HttpResponse(status=413)

    secret = channel.channel_secret
    if not secret:
        logger.error("LINE webhook rejected: channel %s has no usable secret", channel.pk)
        return HttpResponse(status=403)
    if not signature_valid(secret, body, request.headers.get("X-Line-Signature", "")):
        logger.warning("LINE webhook rejected: bad or missing signature (channel %s)", channel.pk)
        return HttpResponse(status=403)

    try:
        payload = json.loads(body)
        events = payload.get("events", [])
        if not isinstance(events, list):
            raise ValueError
    except (ValueError, AttributeError):
        return HttpResponse(status=400)

    for event in events[:100]:
        try:
            handle_event(channel, event)
        except Exception:
            logger.exception("LINE webhook: event handling failed (channel %s)", channel.pk)
    # Always 200 once authenticated, otherwise LINE redelivers.
    return HttpResponse(status=200)


def _first_time(channel, event):
    event_id = str(event.get("webhookEventId") or "")[:64]
    if not event_id:
        return True
    try:
        with transaction.atomic():
            LineWebhookEvent.objects.create(channel=channel, webhook_event_id=event_id)
    except IntegrityError:
        return False
    return True


def _reply(channel, event, text, employee=None, name=""):
    token = event.get("replyToken")
    if token:
        LineClient(channel).reply(token, [text_message(text)], employee=employee, event=name)


def _help_text():
    return _(
        "Welcome to OrvaHR notifications. To connect your account, open "
        "OrvaHR > LINE Notifications, create a link code and send it here."
    )


def handle_event(channel, event):
    if not isinstance(event, dict) or not _first_time(channel, event):
        return
    source = event.get("source") or {}
    if source.get("type") != "user" or not source.get("userId"):
        return  # groups/rooms are not used for linking
    user_id = str(source["userId"])[:64]
    kind = event.get("type")
    link = LineUserLink.objects.filter(channel=channel, line_user_id=user_id).first()

    if kind == "unfollow":
        if link:
            LineUserLink.objects.filter(pk=link.pk).update(is_active=False)
        return

    if kind == "follow":
        if link:
            if not link.is_active:
                LineUserLink.objects.filter(pk=link.pk).update(is_active=True)
            _reply(channel, event, _("Your OrvaHR account is connected."), link.employee, "follow")
        else:
            _reply(channel, event, _help_text(), None, "follow")
        return

    if kind != "message" or (event.get("message") or {}).get("type") != "text":
        return

    code = linking.normalise((event.get("message") or {}).get("text", ""))
    if code is None:
        if link and link.is_active:
            text = _("This account only sends OrvaHR notifications. Please use OrvaHR for requests.")
        else:
            text = _help_text()
        _reply(channel, event, text, link.employee if link else None, "message")
        return

    outcome, new_link = linking.redeem(channel, user_id, code)
    messages = {
        linking.LINKED: _("Done. Your OrvaHR account is now connected to LINE."),
        linking.INVALID: _("That link code is not valid. Please create a new one in OrvaHR."),
        linking.EXPIRED: _("That link code has expired. Please create a new one in OrvaHR."),
        linking.USED: _("That link code was already used. Please create a new one in OrvaHR."),
        linking.RATE_LIMITED: _("Too many attempts. Please wait 15 minutes and try again."),
        linking.TAKEN: _("This LINE account is already connected to another OrvaHR employee."),
    }
    _reply(
        channel,
        event,
        messages[outcome],
        new_link.employee if new_link else None,
        "link." + outcome,
    )
