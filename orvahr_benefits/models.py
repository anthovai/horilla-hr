"""
Provident fund, severance / final settlement, and court- or fund-ordered
deductions (student loan กยศ., salary garnishment อายัดเงินเดือน).

Statutory figures (severance bands, PVD rate range, garnishment limits) are
stored as DATA rows or fields, never as constants in the calculations, and
every one of them is marked "verify against current law": this module does
not claim any figure is authoritative.

PDPA: a deduction order's reference / case number is personal data. It is
masked in ``__str__``, lists and exports, never put in a URL or a payroll
component title, and never logged.
"""

from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from base.horilla_company_manager import HorillaCompanyManager
from base.models import Company
from employee.models import Employee
from horilla.models import HorillaModel

VERIFY_NOTE = _("Verify against current law before relying on this figure.")

ZERO = Decimal("0.00")
CENT = Decimal("0.01")


def money(value):
    """Round to satang, half up (Decimal in, Decimal out)."""
    return Decimal(value or 0).quantize(CENT, rounding=ROUND_HALF_UP)


def percent_field(verbose_name, default=ZERO, help_text=""):
    return models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=default,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        verbose_name=verbose_name,
        help_text=help_text,
    )


def money_field(verbose_name, default=ZERO, null=False, help_text=""):
    return models.DecimalField(
        max_digits=14,
        decimal_places=2,
        default=None if null else default,
        null=null,
        blank=null,
        verbose_name=verbose_name,
        help_text=help_text,
    )


EMPLOYEE_COMPANY_PATH = "employee__employee_work_info__company_id"


def mask_reference(value):
    """Show only the last 3 characters of a reference / case number."""
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 3:
        return "*" * len(value)
    return "*" * (len(value) - 3) + value[-3:]


# ---------------------------------------------------------------- provident fund


class ProvidentFundPlan(HorillaModel):
    """A registered provident fund (กองทุนสำรองเลี้ยงชีพ) the company offers."""

    VESTING_EMPLOYMENT = "employment"
    VESTING_MEMBERSHIP = "membership"
    VESTING_BASIS_CHOICES = [
        (VESTING_EMPLOYMENT, _("Years of employment (from joining date)")),
        (VESTING_MEMBERSHIP, _("Years of fund membership")),
    ]

    company_id = models.ForeignKey(
        Company,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        verbose_name=_("Company"),
    )
    name = models.CharField(max_length=150, verbose_name=_("Plan name"))
    fund_manager_name = models.CharField(
        max_length=150, verbose_name=_("Fund manager")
    )
    policy_name = models.CharField(
        max_length=150, blank=True, default="", verbose_name=_("Investment policy")
    )
    fund_registration_no = models.CharField(
        max_length=60,
        blank=True,
        default="",
        verbose_name=_("Fund registration number"),
    )
    min_employee_rate = percent_field(
        _("Minimum employee rate (%)"),
        Decimal("2.00"),
        _(
            "Commonly cited statutory range is 2%-15% of wages. Verify against "
            "current law before relying on this figure."
        ),
    )
    max_employee_rate = percent_field(_("Maximum employee rate (%)"), Decimal("15.00"))
    default_employee_rate = percent_field(
        _("Default employee rate (%)"), Decimal("3.00")
    )
    default_employer_rate = percent_field(
        _("Default employer rate (%)"),
        Decimal("3.00"),
        _(
            "Commonly cited rule: the employer contributes no less than the "
            "employee. Verify against current law before relying on this figure."
        ),
    )
    vesting_basis = models.CharField(
        max_length=20,
        choices=VESTING_BASIS_CHOICES,
        default=VESTING_EMPLOYMENT,
        verbose_name=_("Vesting counts"),
    )
    notes = models.TextField(blank=True, default="", verbose_name=_("Notes"))

    objects = HorillaCompanyManager("company_id")

    class Meta:
        verbose_name = _("Provident Fund Plan")
        verbose_name_plural = _("Provident Fund Plans")
        ordering = ["name"]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.min_employee_rate > self.max_employee_rate:
            raise ValidationError(
                {"max_employee_rate": _("The maximum must not be below the minimum.")}
            )
        if not (
            self.min_employee_rate
            <= self.default_employee_rate
            <= self.max_employee_rate
        ):
            raise ValidationError(
                {"default_employee_rate": _("The default must be within the range.")}
            )

    def vested_percent(self, years_of_service):
        """
        Percent of the employer's contributions (and their returns) vested
        after ``years_of_service`` (a Decimal, fractional years allowed).

        The step with the largest ``years_of_service`` not above the service
        applies; below the first step nothing is vested.
        """
        years = Decimal(str(years_of_service or 0))
        step = (
            self.vesting_steps.filter(years_of_service__lte=years)
            .order_by("-years_of_service")
            .first()
        )
        return step.vested_percent if step else ZERO


class PVDVestingStep(models.Model):
    """One row of a plan's vesting schedule: after N years, X% is vested."""

    plan = models.ForeignKey(
        ProvidentFundPlan, on_delete=models.CASCADE, related_name="vesting_steps"
    )
    years_of_service = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
        verbose_name=_("Years of service (at least)"),
    )
    vested_percent = percent_field(_("Employer share vested (%)"))

    class Meta:
        verbose_name = _("Vesting step")
        verbose_name_plural = _("Vesting steps")
        ordering = ["plan", "years_of_service"]
        constraints = [
            models.UniqueConstraint(
                fields=["plan", "years_of_service"], name="unique_pvd_vesting_step"
            )
        ]

    def __str__(self):
        return f"{self.years_of_service}y -> {self.vested_percent}%"


class EmployeeProvidentFund(HorillaModel):
    """An employee's membership of a provident fund plan."""

    employee = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        related_name="provident_funds",
        verbose_name=_("Employee"),
    )
    plan = models.ForeignKey(
        ProvidentFundPlan,
        on_delete=models.PROTECT,
        related_name="members",
        verbose_name=_("Plan"),
    )
    employee_rate = percent_field(_("Employee rate (%)"))
    employer_rate = percent_field(_("Employer rate (%)"))
    joined_on = models.DateField(verbose_name=_("Joined fund on"))
    left_on = models.DateField(null=True, blank=True, verbose_name=_("Left fund on"))
    opening_employee_balance = money_field(
        _("Opening employee balance"),
        help_text=_(
            "Contributions made before OrvaHR payroll was used (from the fund "
            "manager's statement). Leave 0 for new members."
        ),
    )
    opening_employer_balance = money_field(_("Opening employer balance"))

    objects = HorillaCompanyManager(EMPLOYEE_COMPANY_PATH)

    class Meta:
        verbose_name = _("Provident Fund Member")
        verbose_name_plural = _("Provident Fund Members")
        ordering = ["-joined_on"]
        constraints = [
            models.UniqueConstraint(
                fields=["employee"],
                condition=Q(left_on__isnull=True),
                name="unique_open_pvd_membership",
            )
        ]

    def __str__(self):
        return f"{self.employee} - {self.plan}"

    def clean(self):
        super().clean()
        errors = {}
        plan = getattr(self, "plan", None) if self.plan_id else None
        if plan is not None and not (
            plan.min_employee_rate <= self.employee_rate <= plan.max_employee_rate
        ):
            errors["employee_rate"] = _(
                "The employee rate must be between %(min)s%% and %(max)s%% for this plan."
            ) % {"min": plan.min_employee_rate, "max": plan.max_employee_rate}
        if self.left_on and self.joined_on and self.left_on < self.joined_on:
            errors["left_on"] = _("The leaving date is before the joining date.")
        if errors:
            raise ValidationError(errors)

    def is_member_on(self, day):
        return self.joined_on <= day and (self.left_on is None or self.left_on >= day)


class PVDDeductionLink(models.Model):
    """
    The payroll Deduction this app maintains for one (plan, employee rate,
    employer rate) combination. ``specific_employees`` of that Deduction is
    kept equal to the plan's current members at those rates by
    ``services.pvd.sync_pvd_deductions``.
    """

    plan = models.ForeignKey(
        ProvidentFundPlan, on_delete=models.CASCADE, related_name="deduction_links"
    )
    employee_rate = models.DecimalField(max_digits=5, decimal_places=2)
    employer_rate = models.DecimalField(max_digits=5, decimal_places=2)
    deduction = models.OneToOneField(
        "payroll.Deduction", on_delete=models.CASCADE, related_name="pvd_link"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["plan", "employee_rate", "employer_rate"],
                name="unique_pvd_deduction_link",
            )
        ]

    def __str__(self):
        return f"{self.plan} {self.employee_rate}/{self.employer_rate}"


# ---------------------------------------------------------------- severance


class SeveranceRule(HorillaModel):
    """
    One severance band: service of at least ``min_days`` and less than
    ``max_days`` (open-ended when empty) earns ``days_of_wages`` days of the
    last wage rate. Bands with the latest ``effective_from`` on or before the
    last working day are the ones used.

    Seeded with the commonly cited LPA s.118 bands; verify against current
    law before relying on them.
    """

    min_days = models.PositiveIntegerField(verbose_name=_("Service from (days)"))
    max_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name=_("Service below (days)"),
        help_text=_("Leave empty for no upper limit."),
    )
    days_of_wages = models.PositiveIntegerField(verbose_name=_("Days of wages"))
    effective_from = models.DateField(verbose_name=_("Effective from"))
    legal_reference = models.CharField(
        max_length=150, blank=True, default="", verbose_name=_("Legal reference")
    )
    note = models.TextField(blank=True, default="", verbose_name=_("Note"))

    class Meta:
        verbose_name = _("Severance Rule")
        verbose_name_plural = _("Severance Rules")
        ordering = ["-effective_from", "min_days"]

    def __str__(self):
        upper = self.max_days if self.max_days is not None else "∞"
        return f"{self.min_days}-{upper}d: {self.days_of_wages}d ({self.effective_from})"

    def clean(self):
        super().clean()
        if self.max_days is not None and self.max_days <= self.min_days:
            raise ValidationError(
                {"max_days": _("The upper limit must be above the lower limit.")}
            )


class AnnualLeaveType(models.Model):
    """
    Marks a leave type as annual leave (วันหยุดพักผ่อนประจำปี) for the final
    settlement's unused-leave payout. See services.settlement.annual_leave_types.
    """

    leave_type = models.OneToOneField(
        "leave.LeaveType",
        on_delete=models.CASCADE,
        related_name="orvahr_annual_marker",
        verbose_name=_("Leave type"),
    )

    class Meta:
        verbose_name = _("Annual leave type")
        verbose_name_plural = _("Annual leave types")

    def __str__(self):
        return str(self.leave_type)


class FinalSettlement(HorillaModel):
    """
    What is owed to a leaving employee. Components are computed by
    ``services.settlement.compute`` while the settlement is a draft and are
    then frozen: an approved settlement never changes under anyone.
    """

    REASON_RESIGNATION = "resignation"
    REASON_TERMINATION_WITHOUT_CAUSE = "termination_without_cause"
    REASON_TERMINATION_WITH_CAUSE = "termination_with_cause"
    REASON_RETIREMENT = "retirement"
    REASON_CONTRACT_END = "contract_end"
    REASON_CHOICES = [
        (REASON_RESIGNATION, _("Resignation")),
        (REASON_TERMINATION_WITHOUT_CAUSE, _("Termination without cause")),
        (REASON_TERMINATION_WITH_CAUSE, _("Termination with cause")),
        (REASON_RETIREMENT, _("Retirement")),
        (REASON_CONTRACT_END, _("End of contract")),
    ]

    STATUS_DRAFT = "draft"
    STATUS_APPROVED = "approved"
    STATUS_PAID = "paid"
    STATUS_CHOICES = [
        (STATUS_DRAFT, _("Draft")),
        (STATUS_APPROVED, _("Approved")),
        (STATUS_PAID, _("Paid")),
    ]

    employee = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        related_name="final_settlements",
        verbose_name=_("Employee"),
    )
    last_working_day = models.DateField(verbose_name=_("Last working day"))
    reason = models.CharField(
        max_length=40, choices=REASON_CHOICES, verbose_name=_("Reason for leaving")
    )
    notice_given_days = models.PositiveIntegerField(
        default=0, verbose_name=_("Notice given (days)")
    )
    required_notice_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name=_("Required notice (days)"),
        help_text=_("Leave empty to use the contract's notice period."),
    )
    daily_wage_override = money_field(
        _("Daily wage (override)"),
        null=True,
        help_text=_(
            "Set this when the wage for severance must include regular "
            "allowances, or for hourly contracts. Empty: worked out from the "
            "contract (monthly wage / 30, or the daily wage)."
        ),
    )
    annual_leave_days_override = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_("Unused annual leave days (override)"),
    )

    # ---- computed (frozen once approved)
    contract = models.ForeignKey(
        "payroll.Contract",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        editable=False,
        related_name="+",
    )
    service_days = models.PositiveIntegerField(default=0, editable=False)
    wage_basis = models.CharField(max_length=40, blank=True, default="", editable=False)
    daily_wage = money_field(_("Daily wage"))
    last_wage_from = models.DateField(null=True, blank=True, editable=False)
    last_wage_days = models.PositiveIntegerField(default=0, editable=False)
    last_wages = money_field(_("Last wages (pro-rata)"))
    severance_days = models.PositiveIntegerField(default=0, editable=False)
    severance_amount = money_field(_("Severance pay"))
    notice_shortfall_days = models.PositiveIntegerField(default=0, editable=False)
    notice_pay = money_field(_("Payment in lieu of notice"))
    annual_leave_days = models.DecimalField(
        max_digits=6, decimal_places=2, default=ZERO, editable=False
    )
    leave_payout = money_field(_("Unused annual leave payout"))
    pvd_vested_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=ZERO, editable=False
    )
    pvd_employer_balance = money_field(_("Provident fund employer balance"))
    pvd_vested_employer = money_field(
        _("Provident fund vested employer share"),
        help_text=_("Paid out by the fund, not by the company; not in the total."),
    )
    computation_notes = models.TextField(blank=True, default="", editable=False)

    # ---- manual
    other_additions = money_field(_("Other additions"))
    other_deductions = money_field(_("Other deductions"))
    other_note = models.TextField(
        blank=True, default="", verbose_name=_("Other items (description)")
    )
    taxable_amount = money_field(
        _("Taxable amount"),
        null=True,
        help_text=_(
            "Severance has special tax rules. Enter the split as advised by "
            "your accountant; OrvaHR does not calculate it."
        ),
    )
    tax_exempt_amount = money_field(_("Tax-exempt amount"), null=True)
    tax_note = models.TextField(blank=True, default="", verbose_name=_("Tax note"))

    total_payable = money_field(_("Total payable by the company"))
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT, editable=False
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        editable=False,
        related_name="+",
    )
    approved_at = models.DateTimeField(null=True, blank=True, editable=False)
    paid_on = models.DateField(null=True, blank=True, editable=False)

    objects = HorillaCompanyManager(EMPLOYEE_COMPANY_PATH)

    class Meta:
        verbose_name = _("Final Settlement")
        verbose_name_plural = _("Final Settlements")
        ordering = ["-last_working_day", "-pk"]
        permissions = [
            ("approve_finalsettlement", _("Can approve final settlements")),
        ]

    def __str__(self):
        return f"{self.employee} - {self.last_working_day}"

    @property
    def is_draft(self):
        return self.status == self.STATUS_DRAFT


# ---------------------------------------------------------------- deduction orders


class DeductionOrder(HorillaModel):
    """
    A deduction the company is ordered to make from an employee's wages and
    remit to a third party: a student loan fund (กยศ.) notice or a court
    garnishment (อายัดเงินเดือน) order.
    """

    TYPE_STUDENT_LOAN = "student_loan"
    TYPE_GARNISHMENT = "garnishment"
    TYPE_CHOICES = [
        (TYPE_STUDENT_LOAN, _("Student loan (กยศ.)")),
        (TYPE_GARNISHMENT, _("Court garnishment")),
    ]

    STATUS_ACTIVE = "active"
    STATUS_SUSPENDED = "suspended"
    STATUS_COMPLETED = "completed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, _("Active")),
        (STATUS_SUSPENDED, _("Suspended")),
        (STATUS_COMPLETED, _("Completed")),
        (STATUS_CANCELLED, _("Cancelled")),
    ]

    employee = models.ForeignKey(
        Employee,
        on_delete=models.PROTECT,
        related_name="deduction_orders",
        verbose_name=_("Employee"),
    )
    order_type = models.CharField(
        max_length=20, choices=TYPE_CHOICES, verbose_name=_("Order type")
    )
    reference_number = models.CharField(
        max_length=100,
        verbose_name=_("Reference / case number"),
        help_text=_("Personal data: shown masked in lists and exports."),
    )
    issuer = models.CharField(
        max_length=150,
        verbose_name=_("Issuer"),
        help_text=_("The fund or court that issued the order, and who is paid."),
    )
    amount_per_month = money_field(_("Amount per month"), null=True)
    percent_of_wage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        verbose_name=_("Percent of gross wage"),
    )
    total_cap = money_field(
        _("Total to deduct"),
        null=True,
        help_text=_("Outstanding amount; deductions stop once it is reached."),
    )
    max_percent_of_wage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("100"))],
        verbose_name=_("Legal cap: max percent of gross wage"),
        help_text=_(
            "Optional ceiling from the order or the law. Verify against "
            "current law before relying on this figure."
        ),
    )
    protected_monthly_amount = money_field(
        _("Legal cap: wage protected per month"),
        null=True,
        help_text=_(
            "Optional amount of monthly wage that may not be deducted (e.g. a "
            "statutory exemption for garnishment). Verify against current law "
            "before relying on this figure."
        ),
    )
    start_date = models.DateField(verbose_name=_("Start date"))
    end_date = models.DateField(null=True, blank=True, verbose_name=_("End date"))
    priority = models.PositiveSmallIntegerField(
        default=10,
        verbose_name=_("Priority"),
        help_text=_("Lower is deducted first when several orders compete."),
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_ACTIVE,
        verbose_name=_("Status"),
    )
    notes = models.TextField(blank=True, default="", verbose_name=_("Notes"))
    deduction = models.OneToOneField(
        "payroll.Deduction",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        editable=False,
        related_name="orvahr_deduction_order",
    )

    objects = HorillaCompanyManager(EMPLOYEE_COMPANY_PATH)

    class Meta:
        verbose_name = _("Deduction Order")
        verbose_name_plural = _("Deduction Orders")
        ordering = ["employee", "priority", "start_date"]

    def __str__(self):
        return f"{self.get_order_type_display()} {self.masked_reference}"

    def __repr__(self):
        return f"<DeductionOrder pk={self.pk} type={self.order_type}>"

    @property
    def masked_reference(self):
        return mask_reference(self.reference_number)

    def clean(self):
        super().clean()
        errors = {}
        has_amount = self.amount_per_month is not None
        has_percent = self.percent_of_wage is not None
        if has_amount == has_percent:
            errors["amount_per_month"] = _(
                "Enter either an amount per month or a percent of wage, not both."
            )
        if has_amount and self.amount_per_month <= 0:
            errors["amount_per_month"] = _("The amount must be more than zero.")
        if has_percent and self.percent_of_wage <= 0:
            errors["percent_of_wage"] = _("The percent must be more than zero.")
        if self.end_date and self.start_date and self.end_date < self.start_date:
            errors["end_date"] = _("The end date is before the start date.")
        if errors:
            raise ValidationError(errors)

    def is_in_force_on(self, day):
        return (
            self.status == self.STATUS_ACTIVE
            and self.start_date <= day
            and (self.end_date is None or self.end_date >= day)
        )
