"""Sidebar entry for orvahr_benefits (auto-discovered by horilla.config.sidebar)."""

from django.urls import reverse
from django.utils.translation import gettext_lazy as _

MENU = _("Thai Benefits")
IMG_SRC = "images/ui/wallet-outline.svg"

SUBMENUS = [
    {
        "menu": _("Provident Fund"),
        "redirect": reverse("orvahr_benefits:pvd-members"),
        "accessibility": "orvahr_benefits.sidebar.pvd_accessibility",
        "match_prefixes": ["/th-benefits/pvd/"],
    },
    {
        "menu": _("Final Settlements"),
        "redirect": reverse("orvahr_benefits:settlements"),
        "accessibility": "orvahr_benefits.sidebar.settlement_accessibility",
        "match_prefixes": ["/th-benefits/settlements/", "/th-benefits/severance-rules/"],
    },
    {
        "menu": _("Loan & Garnishment Orders"),
        "redirect": reverse("orvahr_benefits:orders"),
        "accessibility": "orvahr_benefits.sidebar.order_accessibility",
        "match_prefixes": ["/th-benefits/orders/"],
    },
    {
        "menu": _("My Provident Fund"),
        "redirect": reverse("orvahr_benefits:my-benefits"),
    },
]


def pvd_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("orvahr_benefits.view_employeeprovidentfund")


def settlement_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("orvahr_benefits.view_finalsettlement")


def order_accessibility(request, submenu, user_perms, *args, **kwargs):
    return request.user.has_perm("orvahr_benefits.view_deductionorder")
