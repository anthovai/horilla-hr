"""
Settings sidebar entry for the company tax profile, and a "Thai filings" link
under Payroll in the main sidebar.

The Payroll sidebar is payroll/sidebar.py's SUBMENUS list; it is appended to
at startup (the same self-registration this app uses for its URLs) rather than
editing payroll.
"""

from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _

from horilla.menu import settings_menu

FILING_URL = "/th-filing/"


def tax_profile_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("orvahr_filing.view_companytaxprofile")


def filings_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("payroll.view_payslip")


@settings_menu.register
class OrvahrFilingSettings:
    title = _("Thai statutory filings")
    order = 12
    items = [
        {
            "label": _("Company tax profile"),
            "url": reverse_lazy("orvahr_filing:tax-profile"),
            "accessibility": tax_profile_accessibility,
            "search_entries": [
                {
                    "text": _("Company tax ID"),
                    "description": _(
                        "Legal name, tax ID, branch and social security account of each company"
                    ),
                },
            ],
        },
    ]


def register_payroll_submenu(**_kwargs):
    """
    Add the entry to payroll's sidebar. Runs on the first request (see
    connect()), never at startup: payroll/sidebar.py calls reverse() at import
    time, and importing it from ready() would build Django's URL resolver
    cache before later apps have appended their URL includes, leaving their
    namespaces unresolvable.
    """
    from django.core.signals import request_started

    request_started.disconnect(register_payroll_submenu, dispatch_uid=_DISPATCH_UID)
    try:
        from payroll import sidebar
    except ImportError:
        return
    entry = {
        "menu": _("Thai filings"),
        "redirect": FILING_URL,
        "accessibility": "orvahr_filing.menu.filings_accessibility",
    }
    if not any(item.get("redirect") == FILING_URL for item in sidebar.SUBMENUS):
        sidebar.SUBMENUS.append(entry)


_DISPATCH_UID = "orvahr_filing.payroll_submenu"


def connect():
    """Called from ready(): touches no URL, only arms the first-request hook."""
    from django.core.signals import request_started

    request_started.connect(
        register_payroll_submenu, dispatch_uid=_DISPATCH_UID, weak=False
    )
