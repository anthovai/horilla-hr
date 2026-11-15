"""
Face check-in data.

Biometric data is sensitive personal data under PDPA s.26. What is stored:

* ``FaceSettings``: per-company switches. No personal data.
* ``FaceTemplate``: one *encrypted* face embedding per enrolment (a vector of
  floats produced by services/face-verify). The ciphertext is never shown in
  the admin, a serializer, a form or a log. No image is stored anywhere:
  frames go from the browser to Django to the service in memory and are
  discarded.
* ``FaceAttempt``: one row per enrolment / verification / deletion with the
  outcome, a reason code and the similarity score. No image, no template.
  A successful face check-in links the AttendanceActivity it created, which
  is how "method = face" is recorded for attendance.
"""

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from base.horilla_company_manager import HorillaCompanyManager
from horilla.models import HorillaModel

MAX_ACTIVE_TEMPLATES = 3
DEFAULT_RETENTION_DAYS = 730  # verify with counsel


class FaceSettings(HorillaModel):
    """Face check-in switches, one row per company (no company = default)."""

    company = models.OneToOneField(
        "base.Company",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="orvahr_face_settings",
        verbose_name=_("Company"),
        help_text=_("Leave empty for the default used by companies without their own settings."),
    )
    enabled = models.BooleanField(
        default=False,
        verbose_name=_("Enable face check-in"),
        help_text=_(
            "Employees who gave biometric consent can enrol their face and check in with it. "
            "Turning this on also switches off the legacy photo-upload face detection."
        ),
    )
    required_for_web = models.BooleanField(
        default=False,
        verbose_name=_("Require face for web check-in"),
        help_text=_(
            "Enrolled employees must use face check-in on the web. Employees without biometric "
            "consent keep the normal check-in button: consent is never forced."
        ),
    )
    liveness_required = models.BooleanField(
        default=True,
        verbose_name=_("Require liveness"),
        help_text=_("Refuse a check-in when the face does not look live (e.g. a printed photo or a screen)."),
    )
    match_threshold = models.FloatField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0.3), MaxValueValidator(0.99)],
        verbose_name=_("Match threshold"),
        help_text=_("Similarity needed for a match (0.30-0.99). Leave empty for the face service default."),
    )
    retention_days = models.PositiveSmallIntegerField(
        default=DEFAULT_RETENTION_DAYS,
        validators=[MinValueValidator(30)],
        verbose_name=_("Keep face templates for (days)"),
        help_text=_(
            "Templates older than this are deleted automatically and the employee re-enrols. "
            "Have counsel verify the period."
        ),
    )

    objects = HorillaCompanyManager("company")
    all_objects = models.Manager()

    class Meta:
        verbose_name = _("Face Check-in Settings")
        verbose_name_plural = _("Face Check-in Settings")

    def __str__(self):
        return str(self.company or _("Default"))

    @classmethod
    def for_company(cls, company):
        """The company's own row, else the default row, else None."""
        qs = cls.all_objects.all()
        row = qs.filter(company=company).first() if company else None
        return row or qs.filter(company__isnull=True).first()


class FaceTemplate(models.Model):
    """An encrypted face embedding. Never expose ``template_encrypted``."""

    SOURCE_CHOICES = [
        ("self", _("Self-service")),
        ("hr", _("HR-assisted")),
    ]

    company = models.ForeignKey("base.Company", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    employee = models.ForeignKey(
        "employee.Employee",
        on_delete=models.CASCADE,
        related_name="face_templates",
        verbose_name=_("Employee"),
    )
    template_encrypted = models.TextField(editable=False)
    model_id = models.CharField(max_length=100, verbose_name=_("Recognition model"))
    model_version = models.CharField(max_length=50, blank=True, verbose_name=_("Model version"))
    dims = models.PositiveSmallIntegerField(default=0)
    frames_count = models.PositiveSmallIntegerField(default=0, verbose_name=_("Frames accepted"))
    quality_summary = models.JSONField(default=dict, blank=True, verbose_name=_("Quality summary"))
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default="self", verbose_name=_("Enrolled by"))
    created_at = models.DateTimeField(default=timezone.now, db_index=True, verbose_name=_("Enrolled at"))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    objects = HorillaCompanyManager("employee__employee_work_info__company_id")
    all_objects = models.Manager()

    class Meta:
        verbose_name = _("Face Template")
        verbose_name_plural = _("Face Templates")
        ordering = ["-created_at", "-id"]
        permissions = [
            ("enrol_others_face", _("Can enrol another employee's face (HR-assisted)")),
        ]

    def __str__(self):
        return f"{self.employee} {self.created_at:%Y-%m-%d}"

    def __repr__(self):  # never the ciphertext
        return f"<FaceTemplate {self.pk} employee={self.employee_id} model={self.full_model}>"

    @property
    def full_model(self):
        return f"{self.model_id}@{self.model_version}" if self.model_version else self.model_id


class FaceAttempt(models.Model):
    """Evidence/statistics row. Outcome and reason codes only, no biometric data."""

    KIND_CHOICES = [
        ("enrol", _("Enrolment")),
        ("verify", _("Verification")),
        ("delete", _("Deletion")),
    ]
    DIRECTION_CHOICES = [
        ("", "-"),
        ("in", _("Check-in")),
        ("out", _("Check-out")),
    ]
    OUTCOME_CHOICES = [
        ("success", _("Success")),
        ("no_match", _("Face did not match")),
        ("not_live", _("Liveness check failed")),
        ("no_face", _("No face found")),
        ("multiple_faces", _("More than one face")),
        ("too_small", _("Face too small")),
        ("blurry", _("Image too blurry")),
        ("model_mismatch", _("Re-enrolment needed")),
        ("not_enrolled", _("Not enrolled")),
        ("no_consent", _("No biometric consent")),
        ("rate_limited", _("Too many attempts")),
        ("invalid_image", _("Invalid image")),
        ("service_unavailable", _("Face service unavailable")),
        ("attendance_refused", _("Attendance rules refused the punch")),
        ("deleted", _("Templates deleted")),
    ]
    METHOD_FACE = "face"

    company = models.ForeignKey("base.Company", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    employee = models.ForeignKey(
        "employee.Employee",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="face_attempts",
        verbose_name=_("Employee"),
    )
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, verbose_name=_("Kind"))
    direction = models.CharField(max_length=3, choices=DIRECTION_CHOICES, blank=True, default="")
    outcome = models.CharField(max_length=30, choices=OUTCOME_CHOICES, verbose_name=_("Outcome"))
    detail = models.CharField(max_length=100, blank=True, verbose_name=_("Detail"))
    score = models.FloatField(null=True, blank=True)
    method = models.CharField(max_length=10, default=METHOD_FACE, editable=False)
    attendance_activity = models.ForeignKey(
        "attendance.AttendanceActivity",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="face_attempts",
        verbose_name=_("Attendance activity"),
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True, verbose_name=_("At"))

    objects = HorillaCompanyManager("company")
    all_objects = models.Manager()

    class Meta:
        verbose_name = _("Face Attempt")
        verbose_name_plural = _("Face Attempts")
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["employee", "kind", "created_at"], name="orvahr_face_emp_kind_at")]

    def __str__(self):
        return f"{self.get_kind_display()} {self.get_outcome_display()}"


def activity_method(activity):
    """'face' when ``activity`` (an AttendanceActivity) was punched by face check-in, else ''."""
    if activity is None or not getattr(activity, "pk", None):
        return ""
    found = FaceAttempt.all_objects.filter(
        attendance_activity_id=activity.pk, kind="verify", outcome="success"
    ).exists()
    return FaceAttempt.METHOD_FACE if found else ""
