from django import forms
from django.core.validators import MaxLengthValidator

from employee.thai_validators import normalize_digits
from orvahr_filing.models import CompanyTaxProfile

# Typed with dashes or spaces the way they are printed ("0-1055-12345-67-8");
# stored as digits. The model's own validators check the digits.
DIGIT_FIELDS = ("tax_id", "branch_no", "sso_account_no", "sso_branch_no", "postcode")


class CompanyTaxProfileForm(forms.ModelForm):
    class Meta:
        model = CompanyTaxProfile
        fields = [
            "legal_name_th",
            "tax_id",
            "branch_no",
            "rd_branch_type",
            "rd_large_taxpayer",
            "rd_filing_channel",
            "rd_user_id",
            "sso_account_no",
            "sso_branch_no",
            "sso_deduction_code",
            "address_line",
            "subdistrict",
            "district",
            "province",
            "postcode",
            "phone",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name == "rd_large_taxpayer":
                field.widget.attrs.setdefault("class", "oh-switch__checkbox")
            elif name in ("rd_branch_type", "rd_filing_channel"):
                field.widget.attrs.setdefault("class", "oh-select w-100")
            else:
                field.widget.attrs.setdefault("class", "oh-input w-100")
            if name in DIGIT_FIELDS:
                # The separators would otherwise trip the length check before
                # they are stripped.
                field.max_length = None
                field.validators = [
                    v for v in field.validators if not isinstance(v, MaxLengthValidator)
                ]
                field.widget.attrs.pop("maxlength", None)
                field.widget.attrs.setdefault("inputmode", "numeric")
                field.widget.attrs.setdefault("autocomplete", "off")

    def _digits(self, name):
        value = self.cleaned_data.get(name)
        value = normalize_digits(value) if value else value
        if value:
            # HorillaModel.clean_fields() only screens for XSS and never runs
            # field validators, so the checksum / length checks run here.
            CompanyTaxProfile._meta.get_field(name).run_validators(value)
        return value

    def clean_tax_id(self):
        return self._digits("tax_id")

    def clean_branch_no(self):
        return self._digits("branch_no")

    def clean_sso_account_no(self):
        return self._digits("sso_account_no")

    def clean_sso_branch_no(self):
        return self._digits("sso_branch_no")

    def clean_postcode(self):
        return self._digits("postcode")
