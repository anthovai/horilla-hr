"""Registers face check-in settings in the Settings sidebar."""

from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _

from horilla.menu import settings_menu


def settings_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("orvahr_face.view_facesettings")


def enrolments_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("orvahr_face.view_facetemplate")


@settings_menu.register
class FaceCheckInSettingsMenu:
    title = _("Face Check-in")
    order = 6
    items = [
        {
            "label": _("Face check-in settings"),
            "url": reverse_lazy("orvahr_face:settings"),
            "accessibility": settings_accessibility,
            "search_entries": [
                {
                    "text": _("Face check-in"),
                    "description": _("Check in by face with liveness, after biometric consent (PDPA)"),
                },
            ],
        },
        {
            "label": _("Face enrolments"),
            "url": reverse_lazy("orvahr_face:enrolments"),
            "accessibility": enrolments_accessibility,
        },
    ]
