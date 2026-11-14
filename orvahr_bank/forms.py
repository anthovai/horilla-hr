"""Forms for orvahr_bank."""

from django import forms
from django.utils.translation import gettext_lazy as _

from base.models import Company
from horilla.horilla_middlewares import _thread_locals, get_selected_company
from orvahr_bank.models import CompanyBankAccount
from payroll.models.payroll_batch import PayrollBatch

EXPORTABLE_BATCH_STATUSES = (PayrollBatch.APPROVED, PayrollBatch.PAID)


def _writable_companies():
    """The selected company, else the companies the user may see."""
    companies = Company.objects.all()
    selected = get_selected_company()
    if selected and selected != "all":
        return companies.filter(pk=selected)
    request = getattr(_thread_locals, "request", None)
    allowed = getattr(request, "allowed_company_ids", None)
    if allowed is not None:
        return companies.filter(pk__in=allowed)
    return companies


class _Styled:
    def _style(self):
        for f in self.fields.values():
            widget = f.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "oh-checkbox")
            elif isinstance(widget, forms.Select):
                widget.attrs.setdefault("class", "oh-select w-100")
            else:
                widget.attrs.setdefault("class", "oh-input w-100")


class CompanyBankAccountForm(_Styled, forms.ModelForm):
    """
    On edit the account number is not shown back; leaving it empty keeps the
    stored one.
    """

    class Meta:
        model = CompanyBankAccount
        fields = [
            "company_id",
            "name",
            "bank_code",
            "account_number",
            "bank_company_code",
            "default_exporter",
            "is_active",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["company_id"].queryset = _writable_companies()
        self.fields["account_number"].widget = forms.TextInput(
            attrs={"autocomplete": "off", "inputmode": "numeric"}
        )
        if self.instance.pk:
            self.fields["account_number"].required = False
            self.fields["account_number"].help_text = _(
                "Stored as %(masked)s. Leave empty to keep it."
            ) % {"masked": self.instance.masked_account}
            self.initial["account_number"] = ""
        self._style()

    def clean_account_number(self):
        value = self.cleaned_data.get("account_number")
        if not value and self.instance.pk:
            return self.instance.account_number
        return value


class BankExportForm(_Styled, forms.Form):
    payroll_batch = forms.ModelChoiceField(
        queryset=PayrollBatch.objects.none(), label=_("Payroll run")
    )
    bank_account = forms.ModelChoiceField(
        queryset=CompanyBankAccount.objects.none(), label=_("Pay from")
    )
    effective_date = forms.DateField(
        label=_("Effective date"),
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text=_("Defaults to the run's pay date."),
    )
    exclude_problems = forms.BooleanField(
        required=False,
        label=_("Export without the employees listed as problems"),
    )
    fingerprint = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Both managers are company-scoped to the user's selected company.
        self.fields["payroll_batch"].queryset = PayrollBatch.objects.filter(
            status__in=EXPORTABLE_BATCH_STATUSES
        )
        self.fields["bank_account"].queryset = CompanyBankAccount.objects.filter(
            is_active=True
        ).select_related("company_id")
        self._style()

    def clean(self):
        data = super().clean()
        batch, account = data.get("payroll_batch"), data.get("bank_account")
        if (
            batch
            and account
            and batch.company_id_id
            and batch.company_id_id != account.company_id_id
        ):
            raise forms.ValidationError(
                _("The payroll run and the bank account belong to different companies.")
            )
        if batch and not data.get("effective_date"):
            data["effective_date"] = batch.pay_date or batch.period_end
        return data
