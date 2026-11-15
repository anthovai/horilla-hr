"""Forms of the public site (sign-up) and the operator console."""

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from orvahr_saas.models import Plan, Tenant, TenantRequest

PHONE_CHARS = set("0123456789+-() ")


class SignupForm(forms.ModelForm):
    """Trial request. ``website`` is a honeypot: humans never see or fill it."""

    website = forms.CharField(
        required=False,
        label="",
        widget=forms.TextInput(attrs={"autocomplete": "off", "tabindex": "-1"}),
    )
    consent_to_contact = forms.BooleanField(
        required=True,
        label=_(
            "I agree that OrvaHR may use these contact details to contact me "
            "about this request, as described in the privacy notice."
        ),
    )

    class Meta:
        model = TenantRequest
        fields = [
            "company_name",
            "slug",
            "contact_name",
            "contact_email",
            "contact_phone",
            "employee_count",
            "plan",
            "consent_to_contact",
        ]
        labels = {"slug": _("Desired sub-domain")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["plan"].queryset = Plan.objects.filter(is_public=True)
        self.fields["plan"].required = False
        self.fields["plan"].empty_label = _("Not sure yet")
        self.fields["employee_count"].min_value = 1
        self.fields["employee_count"].widget.attrs.update({"min": 1, "max": 100000})
        self.fields["slug"].widget.attrs.update(
            {"autocomplete": "off", "pattern": "[a-z][a-z0-9-]{1,40}", "maxlength": 41}
        )

    def clean_website(self):
        if self.cleaned_data.get("website"):
            raise ValidationError(_("Request rejected."), code="honeypot")
        return ""

    def clean_slug(self):
        from orvahr_saas.services import slug_availability

        slug = (self.cleaned_data.get("slug") or "").strip().lower()
        ok, message = slug_availability(slug)
        if not ok:
            raise ValidationError(message)
        return slug

    def clean_contact_phone(self):
        phone = (self.cleaned_data.get("contact_phone") or "").strip()
        digits = [c for c in phone if c.isdigit()]
        if not set(phone) <= PHONE_CHARS or not 9 <= len(digits) <= 15:
            raise ValidationError(_("Enter a valid phone number."))
        return phone

    def clean_employee_count(self):
        count = self.cleaned_data.get("employee_count")
        if count is not None and not 1 <= count <= 100000:
            raise ValidationError(_("Enter a number between 1 and 100000."))
        return count

    @property
    def is_spam(self):
        return "website" in self.errors


class ConsoleLoginForm(forms.Form):
    username = forms.CharField(label=_("Username"), max_length=150)
    password = forms.CharField(label=_("Password"), widget=forms.PasswordInput)


class ProvisionForm(forms.Form):
    domain = forms.CharField(label=_("Domain"), max_length=253)
    plan = forms.ModelChoiceField(
        label=_("Plan"), queryset=Plan.objects.all(), required=False
    )
    status = forms.ChoiceField(
        label=_("Status"),
        choices=[
            (Tenant.STATUS_TRIAL, _("Trial")),
            (Tenant.STATUS_ACTIVE, _("Active")),
        ],
    )
    thai_setup = forms.BooleanField(
        label=_("Load Thai setup (geography, leave types, holidays)"),
        required=False,
        initial=True,
    )
    admin_password = forms.CharField(
        label=_("Initial administrator password"),
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text=_(
            "Give it to the customer over a separate channel. It is not stored."
        ),
    )

    def __init__(self, *args, tenant_request=None, **kwargs):
        self.tenant_request = tenant_request
        super().__init__(*args, **kwargs)

    def clean_domain(self):
        from orvahr_saas.models import Domain

        domain = self.cleaned_data["domain"].strip().lower()
        if Domain.objects.filter(domain=domain).exists():
            raise ValidationError(_("This domain is already in use."))
        return domain

    def clean_admin_password(self):
        from django.contrib.auth import get_user_model

        password = self.cleaned_data["admin_password"]
        email = self.tenant_request.contact_email if self.tenant_request else ""
        validate_password(password, user=get_user_model()(username=email, email=email))
        return password


class PlanChangeForm(forms.Form):
    plan = forms.ModelChoiceField(label=_("Plan"), queryset=Plan.objects.all())
    max_employees = forms.IntegerField(
        label=_("Employee limit override"),
        required=False,
        min_value=1,
        help_text=_("Leave empty to use the plan's limit."),
    )


class SuspendForm(forms.Form):
    reason = forms.CharField(label=_("Reason"), max_length=255)


class TwoFactorCodeForm(forms.Form):
    code = forms.CharField(
        label=_("Code"),
        max_length=32,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "one-time-code",
                "inputmode": "text",
                "autofocus": True,
            }
        ),
        help_text=_(
            "The 6-digit code from your authenticator app, or one of your "
            "recovery codes."
        ),
    )


class ExtendTrialForm(forms.Form):
    until = forms.DateField(
        label=_("Trial ends on"),
        widget=forms.DateInput(attrs={"type": "date"}),
    )


class PlanRequestDecisionForm(forms.Form):
    """Operator decision on a tenant's plan change request (orvahr_account)."""

    note = forms.CharField(label=_("Note"), max_length=500, required=False)
    apply = forms.BooleanField(label=_("Apply the new plan now"), required=False)
