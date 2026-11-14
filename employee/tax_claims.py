"""
Tax allowance codes an employee can claim, as the payroll engine knows them.

These codes MUST match the ``tax.allowances`` codes in
services/payroll-engine/rules/*.json. The engine rejects an unknown code with
HTTP 422 (and in active mode that fails the payslip loudly), so a code added
here that the rules do not have breaks payroll. The ``personal`` allowance is
fixed in the rules and is never claimed per employee, so it is not listed.
Whether a figure is right is the rules' business and needs an accountant's
sign-off; this module only says which codes exist and what shape they take.
"""

from django.utils.translation import gettext_lazy as _

# Claimed as a count of people (``units``); the engine multiplies by the rate.
PER_UNIT_CODES = ("spouse", "child", "child_extra", "parent")
# Claimed as an amount actually paid, in baht (``amount``); the engine caps it.
CAPPED_CODES = (
    "life_insurance",
    "health_insurance",
    "provident_fund",
    "rmf",
    "home_loan_interest",
)

CLAIM_CODE_CHOICES = [
    ("spouse", _("Spouse")),
    ("child", _("Children")),
    ("child_extra", _("Additional children (second child onward, born 2018 or later)")),
    ("parent", _("Parents")),
    ("life_insurance", _("Life insurance premiums")),
    ("health_insurance", _("Health insurance premiums")),
    ("provident_fund", _("Provident fund contributions")),
    ("rmf", _("RMF investment")),
    ("home_loan_interest", _("Home loan interest")),
]


def is_per_unit(code):
    return code in PER_UNIT_CODES
