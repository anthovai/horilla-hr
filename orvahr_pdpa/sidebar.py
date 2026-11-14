"""
Main sidebar entry. Only loaded when "orvahr_pdpa" is listed in
settings.SIDEBARS (horilla/settings/base.py), which this app does not edit.
"""

from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _

MENU = _("PDPA")
IMG_SRC = "images/ui/report.svg"
ACCESSIBILITY = "orvahr_pdpa.sidebar.menu_accessibility"

SUBMENUS = [
    {"menu": _("Overview"), "redirect": reverse_lazy("orvahr_pdpa:dashboard"),
     "accessibility": "orvahr_pdpa.sidebar.officer"},
    {"menu": _("Data Subject Requests"), "redirect": reverse_lazy("orvahr_pdpa:dsr-list"),
     "accessibility": "orvahr_pdpa.sidebar.dsr"},
    {"menu": _("Consents"), "redirect": reverse_lazy("orvahr_pdpa:consent-status"),
     "accessibility": "orvahr_pdpa.sidebar.consents"},
    {"menu": _("Breach Register"), "redirect": reverse_lazy("orvahr_pdpa:breach-list"),
     "accessibility": "orvahr_pdpa.sidebar.breaches"},
    {"menu": _("Retention"), "redirect": reverse_lazy("orvahr_pdpa:retention-report"),
     "accessibility": "orvahr_pdpa.sidebar.retention"},
    {"menu": _("Sensitive Access Log"), "redirect": reverse_lazy("orvahr_pdpa:access-log"),
     "accessibility": "orvahr_pdpa.sidebar.access_log"},
    {"menu": _("My Privacy"), "redirect": reverse_lazy("orvahr_pdpa:my-privacy")},
]


def menu_accessibility(request, *args, **kwargs):
    return request.user.is_authenticated


def _perm(codename):
    return lambda request, *args, **kwargs: request.user.has_perm(f"orvahr_pdpa.{codename}")


officer = _perm("pdpa_officer")
dsr = _perm("view_datasubjectrequest")
consents = _perm("view_consentrecord")
breaches = _perm("view_breachincident")
retention = _perm("view_retentionpolicy")
access_log = _perm("view_sensitiveaccesslog")
