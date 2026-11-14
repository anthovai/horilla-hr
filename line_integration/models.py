"""
LINE Messaging API integration models.

Plain ``models.Model`` (not HorillaModel) on purpose: HorillaModel carries an
audit-history field, and credential columns must not end up copied into
history/audit tables.
"""

import uuid

from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from base.horilla_company_manager import HorillaCompanyManager
from line_integration import crypto


class LineChannel(models.Model):
    """One LINE Official Account (Messaging API channel) per company."""

    name = models.CharField(max_length=100, verbose_name=_("Name"))
    company = models.OneToOneField(
        "base.Company",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="line_channel",
        verbose_name=_("Company"),
        help_text=_("Leave empty to use this channel for companies without their own."),
    )
    basic_id = models.CharField(
        max_length=40,
        blank=True,
        default="",
        verbose_name=_("LINE Basic ID"),
        help_text=_("The official account ID, e.g. @123abcde. Shown to employees so they can add it as a friend."),
    )
    public_base_url = models.URLField(
        blank=True,
        default="",
        verbose_name=_("Public OrvaHR URL"),
        help_text=_("e.g. https://hr.example.co.th - used for the webhook URL and links in messages."),
    )
    # Ciphertext only. Use the access_token / channel_secret properties.
    access_token_encrypted = models.TextField(blank=True, default="", editable=False)
    channel_secret_encrypted = models.TextField(blank=True, default="", editable=False)
    webhook_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    is_active = models.BooleanField(default=True, verbose_name=_("Active"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Settings UI is scoped to the selected company; the webhook, dispatcher
    # and link-code lookup run outside a user session and use all_objects.
    objects = HorillaCompanyManager("company")
    all_objects = models.Manager()

    class Meta:
        verbose_name = _("LINE Channel")
        verbose_name_plural = _("LINE Channels")
        ordering = ["id"]

    def __str__(self):
        return self.name

    # Credentials ---------------------------------------------------------
    @property
    def access_token(self):
        return crypto.decrypt(self.access_token_encrypted)

    @access_token.setter
    def access_token(self, value):
        self.access_token_encrypted = crypto.encrypt(value)

    @property
    def channel_secret(self):
        return crypto.decrypt(self.channel_secret_encrypted)

    @channel_secret.setter
    def channel_secret(self, value):
        self.channel_secret_encrypted = crypto.encrypt(value)

    def masked_access_token(self):
        return crypto.mask(self.access_token)

    def masked_channel_secret(self):
        return crypto.mask(self.channel_secret)

    @property
    def is_configured(self):
        return bool(self.access_token and self.channel_secret)

    # URLs ----------------------------------------------------------------
    def webhook_path(self):
        return reverse("line-webhook", kwargs={"webhook_key": self.webhook_key})

    def webhook_url(self, request=None):
        if self.public_base_url:
            return self.public_base_url.rstrip("/") + self.webhook_path()
        if request is not None:
            return request.build_absolute_uri(self.webhook_path())
        return self.webhook_path()

    def absolute_url(self, path):
        if not self.public_base_url:
            return ""
        return self.public_base_url.rstrip("/") + path

    def add_friend_url(self):
        if not self.basic_id:
            return ""
        return "https://line.me/R/ti/p/" + self.basic_id.strip()

    @classmethod
    def for_company(cls, company):
        qs = cls.all_objects.filter(is_active=True)
        channel = qs.filter(company=company).first() if company else None
        return channel or qs.filter(company__isnull=True).first()


class LineUserLink(models.Model):
    """Employee <-> LINE userId (userIds are per provider, so per channel)."""

    employee = models.OneToOneField(
        "employee.Employee", on_delete=models.CASCADE, related_name="line_link"
    )
    channel = models.ForeignKey(LineChannel, on_delete=models.CASCADE, related_name="links")
    line_user_id = models.CharField(max_length=64)
    is_active = models.BooleanField(default=True)
    linked_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["channel", "line_user_id"], name="line_unique_user_per_channel"
            )
        ]

    def __str__(self):
        return f"LINE link for employee {self.employee_id}"


class LineLinkCode(models.Model):
    """
    One-time code an employee sends to the bot to prove the LINE account is
    theirs. Only an HMAC of the code is stored.
    """

    employee = models.ForeignKey(
        "employee.Employee", on_delete=models.CASCADE, related_name="line_link_codes"
    )
    channel = models.ForeignKey(LineChannel, on_delete=models.CASCADE, related_name="link_codes")
    code_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)


class LineLinkAttempt(models.Model):
    """Failed code redemptions, for per-LINE-user rate limiting."""

    channel = models.ForeignKey(LineChannel, on_delete=models.CASCADE)
    line_user_id = models.CharField(max_length=64)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["channel", "line_user_id", "created_at"])]


class LineWebhookEvent(models.Model):
    """Seen webhookEventIds, so redeliveries are processed once."""

    channel = models.ForeignKey(LineChannel, on_delete=models.CASCADE)
    webhook_event_id = models.CharField(max_length=64)
    received_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["channel", "webhook_event_id"], name="line_unique_webhook_event"
            )
        ]


class LineMessageLog(models.Model):
    """Outcome of each outbound call. Deliberately stores no message body."""

    KIND_CHOICES = [("push", "push"), ("reply", "reply")]
    STATUS_CHOICES = [("sent", _("Sent")), ("failed", _("Failed")), ("skipped", _("Skipped"))]

    channel = models.ForeignKey(LineChannel, on_delete=models.SET_NULL, null=True, blank=True)
    employee = models.ForeignKey(
        "employee.Employee", on_delete=models.SET_NULL, null=True, blank=True
    )
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    event = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    error = models.CharField(max_length=255, blank=True, default="")
    line_request_id = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]
