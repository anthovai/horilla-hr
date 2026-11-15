"""
Ready-made salary components.

Every payroll department builds the same dozen components by hand — HRA,
Provident Fund, 401(k), GOSI — and gets the same details slightly wrong each
time: the rate, which base it applies to, whether it comes off before tax, the
ceiling it stops at. Getting one of those wrong is not a cosmetic problem; it
underpays or overcharges everyone on the structure.

These are those components, written out once with the rules that actually
define them. Loading one creates an ordinary Allowance or Deduction that can
then be edited like any other — this is a starting point, not a managed object.
Nothing here is loaded automatically.

The figures are the statutory or conventional ones at the time of writing, and
the rates in particular change: they are a head start, not a substitute for
checking against the current rules for the jurisdiction. Where a figure is a
common convention rather than a legal requirement — the HRA percentages, the
401(k) default — the entry says so.
"""

from django.utils.translation import gettext_lazy as _

EARNING = "allowance"
DEDUCTION = "deduction"

# Each entry: the fields the form would have been filled in with, plus enough
# context for someone to tell whether they want it. `fields` is passed straight
# to the model, so anything valid on Allowance or Deduction works here.
COMPONENT_LIBRARY = [
    # ---------------------------------------------------------------- India
    {
        "key": "in_basic",
        "group": _("India"),
        "kind": EARNING,
        "name": "Basic Pay",
        # Translators: xgettext:no-python-format
        "summary": _("50% of CTC. Marked as basic pay, for a CTC Down structure."),
        "note": _(
            "Only needed when the structure divides a CTC. In a Gross Up "
            "structure basic pay comes from the contract wage instead."
        ),
        "fields": {
            "sequence": 10,
            "is_fixed": False,
            "based_on": "component",
            "percentage_of_code": "CTC",
            "rate": 50.0,
            "is_taxable": True,
            "is_basic_pay": True,
        },
    },
    {
        "key": "in_hra",
        "group": _("India"),
        "kind": EARNING,
        "name": "House Rent Allowance (HRA)",
        # Translators: xgettext:no-python-format
        "summary": _("50% of basic pay."),
        "note": _(
            # Translators: xgettext:no-python-format
            "50% is the usual figure for the metro cities and 40% elsewhere; "
            "it is a convention, not a fixed rule."
        ),
        "fields": {
            "sequence": 20,
            "is_fixed": False,
            "based_on": "basic_pay",
            "rate": 50.0,
            "is_taxable": True,
        },
    },
    {
        "key": "in_da",
        "group": _("India"),
        "kind": EARNING,
        "name": "Dearness Allowance (DA)",
        "summary": _("A percentage of basic pay, revised with inflation."),
        "note": _(
            # Translators: xgettext:no-python-format
            "The rate is set periodically by the employer or by government "
            "notification; 20% here is only a placeholder."
        ),
        "fields": {
            "sequence": 30,
            "is_fixed": False,
            "based_on": "basic_pay",
            "rate": 20.0,
            "is_taxable": True,
        },
    },
    {
        "key": "in_conveyance",
        "group": _("India"),
        "kind": EARNING,
        "name": "Conveyance Allowance",
        "summary": _("A flat 1,600 a month, prorated by calendar days."),
        "fields": {
            "sequence": 40,
            "is_fixed": True,
            "amount": 1600.0,
            "is_taxable": True,
            "maximum_unit": "month_calendar_days",
        },
    },
    {
        "key": "in_special",
        "group": _("India"),
        "kind": EARNING,
        "name": "Special Allowance",
        "summary": _("Whatever is left of the CTC after every other earning."),
        "note": _(
            "Only meaningful in a CTC Down structure, and it has to run last — "
            "it can only see what has already been worked out."
        ),
        "fields": {
            "sequence": 900,
            "is_fixed": False,
            "based_on": "balance",
            "is_taxable": True,
        },
    },
    {
        "key": "in_pf",
        "group": _("India"),
        "kind": DEDUCTION,
        "name": "Provident Fund (PF)",
        # Translators: xgettext:no-python-format
        "summary": _("12% of basic pay, before tax. The employer matches it."),
        "note": _(
            "Statutory contributions are commonly capped at a wage ceiling of "
            "15,000; set a maximum on the component if that applies to you."
        ),
        "fields": {
            "sequence": 100,
            "is_fixed": False,
            "based_on": "basic_pay",
            "rate": 12.0,
            "employer_rate": 12.0,
            "is_pretax": True,
        },
    },
    {
        "key": "in_esi",
        "group": _("India"),
        "kind": DEDUCTION,
        "name": "Employee State Insurance (ESI)",
        # Translators: xgettext:no-python-format
        "summary": _("0.75% of gross, only while gross is at or under 21,000."),
        "note": _(
            "The ceiling is the point of this one: above it the employee is "
            "outside the scheme entirely, which is why it carries a rule "
            "rather than a maximum amount."
        ),
        "fields": {
            "sequence": 110,
            "is_fixed": False,
            "based_on": "gross_pay",
            "rate": 0.75,
            "employer_rate": 3.25,
            "is_pretax": True,
            "if_choice": "gross_pay",
            "if_condition": "le",
            "if_amount": 21000.0,
        },
    },
    {
        "key": "in_pt",
        "group": _("India"),
        "kind": DEDUCTION,
        "name": "Professional Tax",
        "summary": _("A flat 200 a month, the same in every period."),
        "note": _(
            "Levied by the state, so both the amount and the slabs vary; a "
            "flat figure is the common case."
        ),
        "fields": {
            "sequence": 120,
            "is_fixed": True,
            "amount": 200.0,
            "is_pretax": True,
            "maximum_unit": "full_period",
        },
    },
    # ------------------------------------------------------------------- US
    {
        "key": "us_401k",
        "group": _("United States"),
        "kind": DEDUCTION,
        "name": "401(k) Contribution",
        # Translators: xgettext:no-python-format
        "summary": _("5% of gross, before tax."),
        "note": _(
            # Translators: xgettext:no-python-format
            "The rate is whatever the employee elected, so 5% is only a "
            "starting point. Annual IRS limits are not modelled here."
        ),
        "fields": {
            "sequence": 100,
            "is_fixed": False,
            "based_on": "gross_pay",
            "rate": 5.0,
            "is_pretax": True,
        },
    },
    {
        "key": "us_social_security",
        "group": _("United States"),
        "kind": DEDUCTION,
        "name": "Social Security",
        # Translators: xgettext:no-python-format
        "summary": _("6.2% of gross, matched by the employer."),
        "note": _(
            "Applies up to an annual wage base that is raised most years; "
            "that cap is not modelled here."
        ),
        "fields": {
            "sequence": 110,
            "is_fixed": False,
            "based_on": "gross_pay",
            "rate": 6.2,
            "employer_rate": 6.2,
            "is_pretax": True,
        },
    },
    {
        "key": "us_medicare",
        "group": _("United States"),
        "kind": DEDUCTION,
        "name": "Medicare",
        # Translators: xgettext:no-python-format
        "summary": _("1.45% of gross, matched by the employer."),
        "fields": {
            "sequence": 120,
            "is_fixed": False,
            "based_on": "gross_pay",
            "rate": 1.45,
            "employer_rate": 1.45,
            "is_pretax": True,
        },
    },
    # ----------------------------------------------------------------- Gulf
    {
        "key": "gcc_housing",
        "group": _("Gulf"),
        "kind": EARNING,
        "name": "Housing Allowance",
        # Translators: xgettext:no-python-format
        "summary": _("25% of basic pay."),
        "fields": {
            "sequence": 20,
            "is_fixed": False,
            "based_on": "basic_pay",
            "rate": 25.0,
            "is_taxable": True,
        },
    },
    {
        "key": "gcc_transport",
        "group": _("Gulf"),
        "kind": EARNING,
        "name": "Transport Allowance",
        # Translators: xgettext:no-python-format
        "summary": _("10% of basic pay."),
        "fields": {
            "sequence": 30,
            "is_fixed": False,
            "based_on": "basic_pay",
            "rate": 10.0,
            "is_taxable": True,
        },
    },
    {
        "key": "gcc_gosi",
        "group": _("Gulf"),
        "kind": DEDUCTION,
        "name": "GOSI",
        "summary": _("9.75% of basic pay, with the employer paying 11.75%."),
        "note": _(
            "Saudi social insurance. The rates differ for Saudi and non-Saudi "
            "employees and the contribution base is usually basic plus "
            "housing — use a formula component if you need that base."
        ),
        "fields": {
            "sequence": 100,
            "is_fixed": False,
            "based_on": "basic_pay",
            "rate": 9.75,
            "employer_rate": 11.75,
            "is_pretax": True,
        },
    },
    # ------------------------------------------------------------------- UK
    {
        "key": "uk_pension",
        "group": _("United Kingdom"),
        "kind": DEDUCTION,
        "name": "Workplace Pension",
        "summary": _("5% of gross, with the employer adding 3%."),
        "note": _(
            "The auto-enrolment minimum. Real schemes apply it to qualifying "
            "earnings between a lower and upper limit rather than to all of "
            "gross."
        ),
        "fields": {
            "sequence": 100,
            "is_fixed": False,
            "based_on": "gross_pay",
            "rate": 5.0,
            "employer_rate": 3.0,
            "is_pretax": True,
        },
    },
    # ------------------------------------------------------------- Thailand
    # Social security and Labour Protection Act overtime. Every figure below is
    # a statutory one as understood at the time of writing (cross-checked with
    # services/payroll-engine/rules/th-2026.json) -- verify against current
    # law before anyone is paid by it. None of it is authoritative.
    #
    # No Thai "Basic Salary" entry: in a Gross Up structure, the usual Thai
    # arrangement, basic salary IS the contract wage and needs no component;
    # a basic-pay component only matters for CTC Down (see "in_basic"), and a
    # second library entry claiming basic pay would make loading everything
    # create two.
    {
        "key": "th_sso",
        "group": _("Thailand"),
        "kind": DEDUCTION,
        "name": "Social Security Fund (SSO)",
        "summary": _(
            "5% of gross pay, on a wage base held between 1,650 and 17,500 "
            "(so 82.50 to 875 a month). The employer pays the same 5%."
        ),
        "note": _(
            "The 1,650-17,500 base applies 1 Jan 2026 - 31 Dec 2028 (Ministerial "
            "Regulation B.E. 2568, Royal Gazette 12 Dec 2025); it rises to 20,000 "
            "in 2029 and 23,000 in 2032: run `manage.py sync_th_sso_base` (it reads "
            "the engine rule files) to move the component onto the base in force. "
            "Verify the rate and the wage base against current law. Assign it "
            "only to employees registered with the Social Security Office. "
            "Taken after tax: Thai income tax allows the contribution as a "
            "deduction itself, so making it pre-tax would count it twice. The "
            "base assumes monthly pay periods, and gross pay is used as the "
            "wage; exclude anything that is not wages under the Social "
            "Security Act."
        ),
        "fields": {
            "sequence": 100,
            "is_fixed": False,
            "based_on": "gross_pay",
            "rate": 5.0,
            "employer_rate": 5.0,
            "is_pretax": False,
            "contribution_base_min": 1650.0,
            "contribution_base_max": 17500.0,
        },
    },
    {
        "key": "th_ot_workday",
        "group": _("Thailand"),
        "kind": EARNING,
        "name": "Overtime on a working day (1.5x)",
        "summary": _(
            "1.5 times the hourly rate for each approved overtime hour on a "
            "working day."
        ),
        "note": _(
            "Labour Protection Act. The hourly rate is worked out from the "
            "contract wage: a monthly wage over 30 days over 8 normal hours, a "
            "daily wage over 8. Change the hours if the normal working day is "
            "shorter. Verify against current law."
        ),
        "fields": {
            "sequence": 200,
            "is_fixed": False,
            "based_on": "overtime",
            "overtime_calc": "slab",
            "overtime_rate_basis": "wage_derived",
            "overtime_days_per_month": 30.0,
            "overtime_hours_per_day": 8.0,
            "is_taxable": True,
        },
        "slabs": [(0.0, None, 1.5)],
    },
    {
        "key": "th_holiday_monthly",
        "group": _("Thailand"),
        "kind": EARNING,
        "name": "Work on a holiday, monthly-paid (1x, then 3x)",
        "summary": _(
            "For monthly-paid staff: one extra hourly rate for each of the "
            "first 8 hours worked on a public holiday, 3 times it after that."
        ),
        "note": _(
            "Monthly-paid employees are already paid for the holiday, so the "
            "normal hours earn one more hourly rate on top; hours beyond them "
            "are holiday overtime at 3 times. Assign to monthly-paid staff "
            "only. Verify against current law."
        ),
        "fields": {
            "sequence": 210,
            "is_fixed": False,
            "based_on": "holiday_overtime",
            "overtime_calc": "slab",
            "overtime_rate_basis": "wage_derived",
            "overtime_days_per_month": 30.0,
            "overtime_hours_per_day": 8.0,
            "is_taxable": True,
        },
        "slabs": [(0.0, 8.0, 1.0), (8.0, None, 3.0)],
    },
    {
        "key": "th_holiday_daily",
        "group": _("Thailand"),
        "kind": EARNING,
        "name": "Work on a holiday, daily-paid (2x, then 3x)",
        "summary": _(
            "For daily-paid staff: twice the hourly rate for each of the first "
            "8 hours worked on a public holiday, 3 times it after that."
        ),
        "note": _(
            "Daily-paid employees are not otherwise paid for the day, so the "
            "normal hours earn twice the hourly rate. Assign to daily-paid "
            "staff only. Verify against current law."
        ),
        "fields": {
            "sequence": 211,
            "is_fixed": False,
            "based_on": "holiday_overtime",
            "overtime_calc": "slab",
            "overtime_rate_basis": "wage_derived",
            "overtime_days_per_month": 30.0,
            "overtime_hours_per_day": 8.0,
            "is_taxable": True,
        },
        "slabs": [(0.0, 8.0, 2.0), (8.0, None, 3.0)],
    },
    {
        "key": "th_weekly_holiday_monthly",
        "group": _("Thailand"),
        "kind": EARNING,
        "name": "Work on a weekly holiday, monthly-paid (1x, then 3x)",
        "summary": _(
            "The same rates as a public holiday, for approved work on the "
            "weekly day off, for monthly-paid staff."
        ),
        "note": _(
            "The Act treats the weekly holiday like any other holiday. Assign "
            "to monthly-paid staff only. Verify against current law."
        ),
        "fields": {
            "sequence": 220,
            "is_fixed": False,
            "based_on": "week_off_overtime",
            "overtime_calc": "slab",
            "overtime_rate_basis": "wage_derived",
            "overtime_days_per_month": 30.0,
            "overtime_hours_per_day": 8.0,
            "is_taxable": True,
        },
        "slabs": [(0.0, 8.0, 1.0), (8.0, None, 3.0)],
    },
    {
        "key": "th_weekly_holiday_daily",
        "group": _("Thailand"),
        "kind": EARNING,
        "name": "Work on a weekly holiday, daily-paid (2x, then 3x)",
        "summary": _(
            "The same rates as a public holiday, for approved work on the "
            "weekly day off, for daily-paid staff."
        ),
        "note": _(
            "Assign to daily-paid staff only. Verify against current law."
        ),
        "fields": {
            "sequence": 221,
            "is_fixed": False,
            "based_on": "week_off_overtime",
            "overtime_calc": "slab",
            "overtime_rate_basis": "wage_derived",
            "overtime_days_per_month": 30.0,
            "overtime_hours_per_day": 8.0,
            "is_taxable": True,
        },
        "slabs": [(0.0, 8.0, 2.0), (8.0, None, 3.0)],
    },
]


def library_rows(existing_titles=frozenset()):
    """
    The library, flagged with what already exists.

    Matched on title, which is what someone reads. A component renamed after
    loading will look absent and can be loaded again — deliberate, because the
    alternative is a hidden marker that makes a plain Allowance behave unlike
    every other one.
    """
    existing = {title.strip().lower() for title in existing_titles}
    return [
        {
            "key": entry["key"],
            "group": entry["group"],
            "kind": entry["kind"],
            "name": entry["name"],
            "summary": entry["summary"],
            "note": entry.get("note", ""),
            "already_loaded": entry["name"].strip().lower() in existing,
        }
        for entry in COMPONENT_LIBRARY
    ]


def load_components(keys=None):
    """
    Create the chosen components. Returns the ones created.

    Skips anything whose title already exists, so running it twice does not
    produce a second Provident Fund — and so a half-finished selection can be
    retried without cleaning up first.
    """
    from payroll.models.models import Allowance, AllowanceSlab, Deduction

    wanted = set(keys) if keys else {entry["key"] for entry in COMPONENT_LIBRARY}
    created = []

    for entry in COMPONENT_LIBRARY:
        if entry["key"] not in wanted:
            continue

        model = Allowance if entry["kind"] == EARNING else Deduction
        if model.objects.filter(title__iexact=entry["name"]).exists():
            continue

        component = model(title=entry["name"], **entry["fields"])
        component.save()
        # Overtime bands: (from hour, to hour or None, multiplier) of a day.
        for start, end, multiplier in entry.get("slabs", ()):
            AllowanceSlab.objects.create(
                allowance_id=component,
                from_hours=start,
                to_hours=end,
                multiplier=multiplier,
            )
        created.append(component)

    return created
