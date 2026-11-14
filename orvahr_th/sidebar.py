"""
Adds "Certificates" to the Employee sidebar menu, the way biometric/sidebar.py
extends the attendance menu. Imported from OrvahrThConfig.ready().
"""

from django.apps import apps
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _

certificates_submenu = {
    "menu": _("Certificates"),
    "redirect": reverse_lazy("orvahr-th-certificates"),
    "accessibility": "orvahr_th.sidebar.certificates_accessibility",
    "match_prefixes": ["/orvahr-th/certificates/"],
}


def certificates_accessibility(request, submenu, user_perms, *args, **kwargs):
    """Every employee can request their own certificates."""
    return getattr(request.user, "employee_get", None) is not None or (
        request.user.has_perm("orvahr_th.view_employmentcertificate")
    )


if apps.is_installed("employee"):
    from employee.sidebar import SUBMENUS

    if certificates_submenu not in SUBMENUS:
        SUBMENUS.append(certificates_submenu)
