"""
Admin. FaceTemplate is deliberately not registered: its ciphertext must not
be shown or edited anywhere. Enrolments are managed on /face/enrolments/.
"""

from django.contrib import admin

from orvahr_face.models import FaceAttempt, FaceSettings


@admin.register(FaceSettings)
class FaceSettingsAdmin(admin.ModelAdmin):
    list_display = ("company", "enabled", "required_for_web", "liveness_required", "match_threshold")
    fields = ("company", "enabled", "required_for_web", "liveness_required", "match_threshold", "retention_days")


@admin.register(FaceAttempt)
class FaceAttemptAdmin(admin.ModelAdmin):
    list_display = ("created_at", "employee", "kind", "direction", "outcome", "score")
    list_filter = ("kind", "outcome")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
