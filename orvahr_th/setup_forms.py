"""Forms of the Thai company setup wizard. Model forms reuse the owning app's form where one fits."""

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from orvahr_bank.forms import _Styled
from orvahr_bank.models import CompanyBankAccount
from orvahr_benefits.forms import ProvidentFundPlanForm
from orvahr_filing.forms import CompanyTaxProfileForm
from orvahr_pdpa.forms import StyledModelForm as PdpaStyledModelForm
from orvahr_pdpa.models import PdpaSettings
from orvahr_th import setup_wizard


class CompanyStepForm(CompanyTaxProfileForm):
    """The filing app's tax profile form, limited to what a new company needs first."""

    # Filled by the geo picker; the address itself is stored as text.
    th_subdistrict_code = forms.CharField(required=False, widget=forms.HiddenInput)

    class Meta(CompanyTaxProfileForm.Meta):
        fields = [
            "legal_name_th",
            "tax_id",
            "branch_no",
            "sso_account_no",
            "sso_branch_no",
            "address_line",
            "subdistrict",
            "district",
            "province",
            "postcode",
            "phone",
        ]


PAY_DAY_CHOICES = [("", _("Not now"))] + [(str(d), str(d)) for d in range(1, 31)] + [
    ("31", _("Last day of the month"))
]


class PayrollStepForm(forms.Form):
    use_baht = forms.BooleanField(
        required=False, initial=True, label=_("Show pay in Thai baht (฿)")
    )
    pay_day = forms.TypedChoiceField(
        choices=PAY_DAY_CHOICES,
        coerce=int,
        empty_value=None,
        required=False,
        label=_("Pay day (day of the month)"),
    )
    thai_tax_pack = forms.BooleanField(
        required=False,
        initial=True,
        label=_("Give new contracts the Thai income tax pack"),
    )
    apply_to_active = forms.BooleanField(
        required=False,
        initial=True,
        label=_("Also give it to active contracts that have no tax setting yet"),
    )
    load_sso = forms.BooleanField(
        required=False, initial=True, label=_("Load the social security (SSO) deduction")
    )
    ot_keys = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label=_("Overtime and holiday-work pay to load"),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ot_rows = setup_wizard.ot_library_rows()
        self.fields["ot_keys"].choices = [(row["key"], row["name"]) for row in self.ot_rows]
        if not self.is_bound:
            self.fields["ot_keys"].initial = [
                row["key"]
                for row in self.ot_rows
                if row["key"] in setup_wizard.OT_DEFAULT_KEYS and not row["already_loaded"]
            ]
        self.fields["pay_day"].widget.attrs["class"] = "oh-select w-100"


class BankStepForm(_Styled, forms.ModelForm):
    """As orvahr_bank's form: on edit the number is not shown back, and empty keeps it."""

    class Meta:
        model = CompanyBankAccount
        fields = ["name", "bank_code", "account_number", "bank_company_code"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account_number"].widget = forms.TextInput(
            attrs={"autocomplete": "off", "inputmode": "numeric"}
        )
        if self.instance.pk:
            self.fields["account_number"].required = False
            self.fields["account_number"].help_text = _(
                "Stored as %(masked)s. Leave empty to keep it."
            ) % {"masked": self.instance.masked_account}
            self.initial["account_number"] = ""
        elif not self.is_bound:
            self.initial.setdefault("name", _("Payroll account"))
        self._style()

    def clean_account_number(self):
        value = self.cleaned_data.get("account_number")
        if not value and self.instance.pk:
            return self.instance.account_number
        return value


class HolidayStepForm(forms.Form):
    year = forms.TypedChoiceField(coerce=int, label=_("Year"))
    lunar_csv = forms.FileField(
        required=False,
        label=_("Lunar and substitution holidays (CSV, optional)"),
        widget=forms.ClearableFileInput(attrs={"accept": ".csv,text/csv"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        this_year = timezone.localdate().year
        self.fields["year"].choices = [(y, str(y)) for y in (this_year, this_year + 1)]
        self.fields["year"].initial = this_year
        self.fields["year"].widget.attrs["class"] = "oh-select w-100"

    def clean_lunar_csv(self):
        upload = self.cleaned_data.get("lunar_csv")
        if upload and upload.size > 256 * 1024:
            raise forms.ValidationError(_("The file is too large for a holiday list."))
        return upload


class LeaveTypesStepForm(forms.Form):
    leave_types = forms.MultipleChoiceField(
        required=False, widget=forms.CheckboxSelectMultiple, label=_("Leave types")
    )

    def __init__(self, *args, rows=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["leave_types"].choices = [(r["name"], r["name"]) for r in rows]
        if not self.is_bound:
            self.fields["leave_types"].initial = [r["name"] for r in rows if not r["exists"]]


class PvdStepForm(ProvidentFundPlanForm):
    class Meta(ProvidentFundPlanForm.Meta):
        fields = [
            "name",
            "fund_manager_name",
            "policy_name",
            "fund_registration_no",
            "min_employee_rate",
            "max_employee_rate",
            "default_employee_rate",
            "default_employer_rate",
        ]


class DpoStepForm(PdpaStyledModelForm):
    class Meta:
        model = PdpaSettings
        fields = ["dpo_name", "dpo_email", "dpo_phone", "dpo_address"]
        widgets = {"dpo_address": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["dpo_name"].required = True
        self.fields["dpo_email"].required = True


class NoticeStepForm(forms.Form):
    audiences = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple,
        choices=[("employee", _("Employees")), ("candidate", _("Candidates"))],
        initial=["employee"],
        label=_("Publish a privacy notice from the template for"),
    )
