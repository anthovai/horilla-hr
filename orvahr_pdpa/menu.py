"""Registers the PDPA pages in the Settings sidebar."""

from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _

from horilla.menu import settings_menu


def officer_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("orvahr_pdpa.view_pdpasettings")


def pdpa_work_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("orvahr_pdpa.pdpa_officer")


@settings_menu.register
class PdpaSettingsMenu:
    title = _("PDPA")
    order = 12
    items = [
        {
            "label": _("PDPA settings"),
            "url": reverse_lazy("orvahr_pdpa:settings"),
            "accessibility": officer_accessibility,
            "search_entries": [
                {"text": _("Privacy notice"), "description": _("Versioned privacy notices for employees and candidates")},
                {"text": _("Data Protection Officer"), "description": _("DPO contact details and PDPA deadlines")},
                {"text": _("Retention policy"), "description": _("How long personal data is kept")},
            ],
        },
        {
            "label": _("PDPA workspace"),
            "url": reverse_lazy("orvahr_pdpa:dashboard"),
            "accessibility": pdpa_work_accessibility,
            "search_entries": [
                {"text": _("Data subject requests"), "description": _("Access, erasure and other PDPA requests")},
                {"text": _("Breach register"), "description": _("Personal data breaches and notification deadlines")},
            ],
        },
    ]
