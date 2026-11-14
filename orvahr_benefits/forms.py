"""Forms for orvahr_benefits (Horilla look: oh-input / oh-select widgets)."""

from django import forms
from django.forms import inlineformset_factory
from django.utils.translation import gettext_lazy as _

from employee.models import Employee
from orvahr_benefits.models import (
    DeductionOrder,
    EmployeeProvidentFund,
    FinalSettlement,
    ProvidentFundPlan,
    PVDVestingStep,
)


def style(form):
    for field in form.fields.values():
        widget = field.widget
        if isinstance(widget, forms.CheckboxInput):
            continue
        css = "oh-select" if isinstance(widget, forms.Select) else "oh-input"
        widget.attrs["class"] = (widget.attrs.get("class", "") + " " + css + " w-100").strip()
        if isinstance(widget, forms.DateInput):
            widget.input_type = "date"
            widget.format = "%Y-%m-%d"
        if isinstance(widget, forms.Textarea):
            widget.attrs.setdefault("rows", 3)


class StyledModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style(self)
        if "employee" in self.fields:
            # Company-scoped by the selected company (Employee.objects).
            self.fields["employee"].queryset = Employee.objects.filter(
                is_active=True
            ).order_by("employee_first_name", "employee_last_name")


DATE_WIDGET = forms.DateInput(format="%Y-%m-%d")


class ProvidentFundPlanForm(StyledModelForm):
    class Meta:
        model = ProvidentFundPlan
        fields = [
            "name",
            "company_id",
            "fund_manager_name",
            "policy_name",
            "fund_registration_no",
            "min_employee_rate",
            "max_employee_rate",
            "default_employee_rate",
            "default_employer_rate",
            "vesting_basis",
            "notes",
            "is_active",
        ]


VestingStepFormSet = inlineformset_factory(
    ProvidentFundPlan,
    PVDVestingStep,
    fields=["years_of_service", "vested_percent"],
    extra=2,
    can_delete=True,
)


class EmployeeProvidentFundForm(StyledModelForm):
    class Meta:
        model = EmployeeProvidentFund
        fields = [
            "employee",
            "plan",
            "employee_rate",
            "employer_rate",
            "joined_on",
            "left_on",
            "opening_employee_balance",
            "opening_employer_balance",
            "is_active",
        ]
        widgets = {"joined_on": DATE_WIDGET, "left_on": DATE_WIDGET}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["plan"].queryset = ProvidentFundPlan.objects.filter(is_active=True)


class FinalSettlementForm(StyledModelForm):
    class Meta:
        model = FinalSettlement
        fields = [
            "employee",
            "last_working_day",
            "reason",
            "notice_given_days",
            "required_notice_days",
            "daily_wage_override",
            "annual_leave_days_override",
            "other_additions",
            "other_deductions",
            "other_note",
            "taxable_amount",
            "tax_exempt_amount",
            "tax_note",
        ]
        widgets = {"last_working_day": DATE_WIDGET}


class DeductionOrderForm(StyledModelForm):
    class Meta:
        model = DeductionOrder
        fields = [
            "employee",
            "order_type",
            "reference_number",
            "issuer",
            "amount_per_month",
            "percent_of_wage",
            "total_cap",
            "max_percent_of_wage",
            "protected_monthly_amount",
            "start_date",
            "end_date",
            "priority",
            "status",
            "notes",
        ]
        widgets = {
            "start_date": DATE_WIDGET,
            "end_date": DATE_WIDGET,
            # Not echoed back into the page source on edit; blank keeps it.
            "reference_number": forms.PasswordInput(render_value=False),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["reference_number"].required = False
            self.fields["reference_number"].help_text = _(
                "Stored: %(masked)s. Leave blank to keep it."
            ) % {"masked": self.instance.masked_reference}

    def clean_reference_number(self):
        value = (self.cleaned_data.get("reference_number") or "").strip()
        if not value and self.instance.pk:
            return self.instance.reference_number
        if not value:
            raise forms.ValidationError(_("This field is required."))
        return value


class MonthForm(forms.Form):
    month = forms.CharField(
        required=False,
        widget=forms.DateInput(attrs={"type": "month", "class": "oh-input"}),
    )

    def year_month(self, default):
        value = (self.data.get("month") or "").strip()
        try:
            year, month = value.split("-")
            year, month = int(year), int(month)
            if 2000 <= year <= 2200 and 1 <= month <= 12:
                return year, month
        except ValueError:
            pass
        return default
