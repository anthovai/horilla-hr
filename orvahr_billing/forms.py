import os

from django import forms
from django.forms import formset_factory, modelformset_factory
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from orvahr_billing import integration
from orvahr_billing.models import (
    BankAccount,
    BillingProfile,
    DocStatus,
    DocType,
    PaymentStatus,
    PlanPrice,
    SellerSettings,
    Subscription,
)

SLIP_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
SLIP_MAX_BYTES = 5 * 1024 * 1024
LOGO_MAX_BYTES = 1024 * 1024
DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class SellerSettingsForm(forms.ModelForm):
    class Meta:
        model = SellerSettings
        fields = [
            "legal_name_th",
            "legal_name_en",
            "tax_id",
            "branch_code",
            "address_th",
            "address_en",
            "phone",
            "email",
            "vat_registered",
            "vat_rate",
            "payment_terms_days",
            "invoice_footer",
            "logo",
        ]
        widgets = {
            "address_th": forms.Textarea(attrs={"rows": 3}),
            "address_en": forms.Textarea(attrs={"rows": 3}),
            "invoice_footer": forms.Textarea(attrs={"rows": 3}),
        }

    def clean_logo(self):
        logo = self.cleaned_data.get("logo")
        if logo and getattr(logo, "size", 0) > LOGO_MAX_BYTES:
            raise forms.ValidationError(_("The logo must be 1 MB or smaller."))
        if logo and os.path.splitext(logo.name)[1].lower() not in (
            ".png",
            ".jpg",
            ".jpeg",
        ):
            raise forms.ValidationError(_("Upload a PNG or JPEG logo."))
        return logo


BankAccountFormSet = modelformset_factory(
    BankAccount,
    fields=[
        "bank_name",
        "branch",
        "account_name",
        "account_number",
        "is_active",
        "sequence",
    ],
    extra=1,
    can_delete=True,
)


class BillingProfileForm(forms.ModelForm):
    class Meta:
        model = BillingProfile
        fields = [
            "legal_name",
            "is_vat_registered",
            "tax_id",
            "branch_code",
            "address",
            "email",
            "contact_name",
            "phone",
        ]
        widgets = {"address": forms.Textarea(attrs={"rows": 3})}


def _plan_field(initial=None):
    choices = integration.plan_choices()
    if choices:
        return forms.ChoiceField(label=_("Plan"), choices=choices, initial=initial)
    return forms.CharField(label=_("Plan code"), max_length=50, initial=initial)


class SubscriptionForm(forms.ModelForm):
    class Meta:
        model = Subscription
        fields = ["plan_code", "cycle", "start_date", "next_invoice_date", "status"]
        widgets = {"start_date": DATE, "next_invoice_date": DATE}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["plan_code"] = _plan_field(self.initial.get("plan_code"))


class PlanPriceForm(forms.ModelForm):
    class Meta:
        model = PlanPrice
        fields = [
            "plan_code",
            "cycle",
            "base_price",
            "per_employee_price",
            "effective_from",
            "description",
        ]
        widgets = {"effective_from": DATE}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["plan_code"] = _plan_field(self.initial.get("plan_code"))


class InvoiceForm(forms.Form):
    tenant = forms.ModelChoiceField(label=_("Tenant"), queryset=None)
    issue_date = forms.DateField(label=_("Issue date"), widget=DATE)
    due_date = forms.DateField(
        label=_("Due date"),
        widget=DATE,
        required=False,
        help_text=_("Empty: issue date + the seller's payment terms."),
    )
    period_start = forms.DateField(label=_("Period start"), widget=DATE, required=False)
    period_end = forms.DateField(label=_("Period end"), widget=DATE, required=False)
    notes = forms.CharField(
        label=_("Notes"), widget=forms.Textarea(attrs={"rows": 2}), required=False
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from orvahr_saas.models import Tenant

        self.fields["tenant"].queryset = Tenant.objects.exclude(
            schema_name="public"
        ).order_by("name")
        self.fields["issue_date"].initial = timezone.localdate()


class LineForm(forms.Form):
    description = forms.CharField(
        label=_("Description"), max_length=500, required=False
    )
    quantity = forms.DecimalField(
        label=_("Quantity"),
        max_digits=12,
        decimal_places=2,
        min_value=0,
        required=False,
    )
    unit = forms.CharField(label=_("Unit"), max_length=30, required=False)
    unit_price = forms.DecimalField(
        label=_("Unit price"),
        max_digits=14,
        decimal_places=2,
        min_value=0,
        required=False,
    )

    def clean(self):
        data = super().clean()
        if data.get("description"):
            if data.get("unit_price") is None:
                self.add_error("unit_price", _("Enter a unit price."))
            if data.get("quantity") is None:
                data["quantity"] = 1
        return data


class BaseLineFormSet(forms.BaseFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        if not any(f.cleaned_data.get("description") for f in self.forms):
            raise forms.ValidationError(_("Add at least one line."))


LineFormSet = formset_factory(LineForm, formset=BaseLineFormSet, extra=3)


class PaymentForm(forms.Form):
    payment_date = forms.DateField(label=_("Payment date"), widget=DATE)
    amount_received = forms.DecimalField(
        label=_("Amount received (THB)"), max_digits=14, decimal_places=2, min_value=0
    )
    wht_amount = forms.DecimalField(
        label=_("Withholding tax deducted (THB)"),
        max_digits=14,
        decimal_places=2,
        min_value=0,
        initial=0,
        required=False,
    )
    wht_certificate_no = forms.CharField(
        label=_("WHT certificate (50 ทวิ) no."), max_length=100, required=False
    )
    reference = forms.CharField(
        label=_("Transfer reference"), max_length=255, required=False
    )
    bank_account = forms.ModelChoiceField(
        label=_("Received into"), queryset=BankAccount.objects.all(), required=False
    )
    slip = forms.FileField(label=_("Transfer slip"), required=False)
    note = forms.CharField(
        label=_("Note"), widget=forms.Textarea(attrs={"rows": 2}), required=False
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["payment_date"].initial = timezone.localdate()

    def clean_slip(self):
        slip = self.cleaned_data.get("slip")
        if slip:
            if os.path.splitext(slip.name)[1].lower() not in SLIP_EXTENSIONS:
                raise forms.ValidationError(_("Upload a PDF, JPEG or PNG file."))
            if slip.size > SLIP_MAX_BYTES:
                raise forms.ValidationError(_("The slip must be 5 MB or smaller."))
        return slip

    def clean(self):
        data = super().clean()
        if data.get("wht_amount") and not data.get("wht_certificate_no"):
            self.add_error(
                "wht_certificate_no",
                _("Enter the certificate number for the amount withheld."),
            )
        return data


class CreditNoteForm(forms.Form):
    amount = forms.DecimalField(
        label=_("Reduction (THB, excl. VAT)"),
        max_digits=14,
        decimal_places=2,
        min_value=0.01,
    )
    reason = forms.CharField(
        label=_("Reason"), widget=forms.Textarea(attrs={"rows": 2})
    )
    description = forms.CharField(
        label=_("Line description"), max_length=500, required=False
    )
    issue_date = forms.DateField(label=_("Issue date"), widget=DATE)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["issue_date"].initial = timezone.localdate()


class EmailForm(forms.Form):
    to = forms.EmailField(label=_("Send to"), required=False)
    lang = forms.ChoiceField(
        label=_("Language"), choices=[("th", "ไทย"), ("en", "English")], initial="th"
    )


class DocumentFilterForm(forms.Form):
    doc_type = forms.ChoiceField(
        label=_("Type"),
        choices=[("", _("All"))] + list(DocType.choices),
        required=False,
    )
    status = forms.ChoiceField(
        label=_("Status"),
        choices=[("", _("All"))] + list(DocStatus.choices),
        required=False,
    )
    payment_status = forms.ChoiceField(
        label=_("Payment"),
        choices=[("", _("All"))] + list(PaymentStatus.choices),
        required=False,
    )
    overdue = forms.BooleanField(label=_("Overdue only"), required=False)
    tenant = forms.CharField(label=_("Tenant"), required=False)
    q = forms.CharField(label=_("Number or customer"), required=False)
    date_from = forms.DateField(label=_("From"), widget=DATE, required=False)
    date_to = forms.DateField(label=_("To"), widget=DATE, required=False)


class NoticeConfirmForm(forms.Form):
    """Operator confirms a tenant's payment notice (orvahr_account)."""

    bank_account = forms.ModelChoiceField(
        label=_("Received into"), queryset=BankAccount.objects.none()
    )
    payment_date = forms.DateField(
        label=_("Payment date"),
        widget=DATE,
        required=False,
        help_text=_("Leave empty to use the transfer date of the notice."),
    )
    note = forms.CharField(label=_("Note"), max_length=500, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["bank_account"].queryset = BankAccount.objects.filter(
            is_active=True
        )


class NoticeRejectForm(forms.Form):
    reason = forms.CharField(label=_("Reason"), max_length=500)
