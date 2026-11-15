"""Forms of the tenant subscription pages."""

from __future__ import annotations

import os
from decimal import Decimal

from django import forms
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from employee.thai_validators import normalize_digits, validate_thai_national_id
from orvahr_account.models import PaymentNotice

DATE = forms.DateInput(attrs={"type": "date", "class": "oh-input w-100"})

# Slip uploads: extension -> (content type, magic bytes it must start with).
SLIP_TYPES = {
    ".pdf": ("application/pdf", (b"%PDF-",)),
    ".png": ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    ".jpg": ("image/jpeg", (b"\xff\xd8\xff",)),
    ".jpeg": ("image/jpeg", (b"\xff\xd8\xff",)),
}
DEFAULT_SLIP_MAX_BYTES = 5 * 1024 * 1024


def slip_max_bytes():
    return int(getattr(settings, "ORVAHR_ACCOUNT_SLIP_MAX_BYTES", DEFAULT_SLIP_MAX_BYTES))


def check_slip(upload):
    """Return the content type of an acceptable slip, or raise ValidationError."""
    limit = slip_max_bytes()
    if upload.size > limit:
        raise forms.ValidationError(
            _("The slip may be at most %(mb)s MB.")
            % {"mb": round(limit / (1024 * 1024), 1)}
        )
    ext = os.path.splitext(upload.name or "")[1].lower()
    kind = SLIP_TYPES.get(ext)
    if kind is None:
        raise forms.ValidationError(_("Upload the slip as a PDF, PNG or JPEG file."))
    content_type, magics = kind
    upload.seek(0)
    head = upload.read(16)
    upload.seek(0)
    if not any(head.startswith(m) for m in magics):
        raise forms.ValidationError(
            _("The file is not a valid PDF, PNG or JPEG file.")
        )
    return content_type


def billing_profile_form_class():
    """
    ModelForm over orvahr_billing.BillingProfile, built on first use:
    orvahr_billing is installed only in SaaS mode, so this module must import
    without it. ``tenant`` is set by the view, never posted.
    """
    global _BillingProfileForm
    if _BillingProfileForm is not None:
        return _BillingProfileForm
    from orvahr_billing.models import BillingProfile

    class BillingProfileForm(forms.ModelForm):
        # Wider than the 13-digit column so "0-1055-56098-76-5" can be typed;
        # clean_tax_id() normalises and checks it, the model validates again.
        tax_id = forms.CharField(
            label=BillingProfile._meta.get_field("tax_id").verbose_name,
            max_length=20,
            required=False,
        )

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

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            for field in self.fields.values():
                if not isinstance(field.widget, forms.CheckboxInput):
                    field.widget.attrs.setdefault("class", "oh-input w-full")

        def clean_tax_id(self):
            value = normalize_digits(self.cleaned_data.get("tax_id")) or ""
            if value:
                validate_thai_national_id(value)  # 13 digits + mod-11 check digit
            return value

    _BillingProfileForm = BillingProfileForm
    return BillingProfileForm


_BillingProfileForm = None


class PaymentNoticeForm(forms.Form):
    invoice = forms.ChoiceField(label=_("Invoice"))
    amount = forms.DecimalField(
        label=_("Amount transferred (THB)"),
        max_digits=14,
        decimal_places=2,
        min_value=Decimal("0.01"),
    )
    transfer_date = forms.DateField(label=_("Transfer date"), widget=DATE)
    reference = forms.CharField(
        label=_("Transfer reference"), max_length=255, required=False
    )
    wht_amount = forms.DecimalField(
        label=_("Withholding tax deducted (THB)"),
        max_digits=14,
        decimal_places=2,
        min_value=Decimal("0"),
        required=False,
    )
    wht_certificate_no = forms.CharField(
        label=_("WHT certificate (50 ทวิ) no."), max_length=100, required=False
    )
    slip = forms.FileField(
        label=_("Transfer slip"),
        help_text=_("PDF, PNG or JPEG, up to 5 MB."),
        widget=forms.ClearableFileInput(attrs={"accept": ".pdf,.png,.jpg,.jpeg"}),
    )
    note = forms.CharField(
        label=_("Note"), required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args, invoices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.invoices = {doc.number: doc for doc in invoices}
        self.fields["invoice"].choices = [
            (
                doc.number,
                f"{doc.number} - {doc.outstanding:,.2f} THB",
            )
            for doc in invoices
        ]
        for name, field in self.fields.items():
            if name != "slip":
                field.widget.attrs.setdefault("class", "oh-input w-full")

    def clean_transfer_date(self):
        value = self.cleaned_data["transfer_date"]
        if value > timezone.localdate():
            raise forms.ValidationError(_("The transfer date cannot be in the future."))
        return value

    def clean_slip(self):
        upload = self.cleaned_data["slip"]
        self.slip_content_type = check_slip(upload)
        return upload

    def clean(self):
        data = super().clean()
        invoice = self.invoices.get(data.get("invoice"))
        amount = data.get("amount")
        wht = data.get("wht_amount") or Decimal("0")
        data["wht_amount"] = wht
        if wht and not data.get("wht_certificate_no"):
            self.add_error(
                "wht_certificate_no",
                _("Enter the 50 ทวิ certificate number for the tax withheld."),
            )
        if invoice is not None and amount is not None:
            if amount + wht > invoice.outstanding:
                self.add_error(
                    "amount",
                    _("Amount plus withholding tax is more than the %(out)s outstanding.")
                    % {"out": f"{invoice.outstanding:,.2f}"},
                )
            elif PaymentNotice.objects.filter(
                invoice_number=invoice.number, status=PaymentNotice.STATUS_PENDING
            ).exists():
                self.add_error(
                    "invoice",
                    _("A payment for this invoice is already waiting for confirmation."),
                )
        data["invoice_document"] = invoice
        return data


class PlanChangeForm(forms.Form):
    plan = forms.ChoiceField(label=_("New plan"))
    note = forms.CharField(
        label=_("Note"),
        required=False,
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    def __init__(self, *args, plans=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.plans = {plan.code: plan for plan in plans}
        self.fields["plan"].choices = [(p.code, p.display_name) for p in plans]
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "oh-input w-full")
