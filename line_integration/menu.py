"""Registers the LINE settings page in the Settings sidebar (Integrations area)."""

from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _

from horilla.menu import settings_menu


def line_settings_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("line_integration.add_linechannel")


@settings_menu.register
class LineIntegrationSettings:
    title = _("LINE")
    order = 11
    items = [
        {
            "label": _("LINE Official Account"),
            "url": reverse_lazy("line-settings"),
            "accessibility": line_settings_accessibility,
            "search_entries": [
                {
                    "text": _("LINE"),
                    "description": _("Send HR notifications through a LINE Official Account"),
                },
                {
                    "text": _("Channel access token"),
                    "description": _("LINE Messaging API credentials and webhook URL"),
                },
            ],
        },
    ]
