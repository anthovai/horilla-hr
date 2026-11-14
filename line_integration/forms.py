from django import forms
from django.utils.translation import gettext_lazy as _

from line_integration.models import LineChannel


class LineChannelForm(forms.ModelForm):
    """
    Credentials are write-only: never rendered back (render_value=False),
    and leaving them blank on edit keeps the stored value.
    """

    access_token = forms.CharField(
        label=_("Channel access token (long-lived)"),
        required=False,
        strip=True,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "off", "class": "oh-input w-100"}),
    )
    channel_secret = forms.CharField(
        label=_("Channel secret"),
        required=False,
        strip=True,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "off", "class": "oh-input w-100"}),
    )

    class Meta:
        model = LineChannel
        fields = ["name", "company", "basic_id", "public_base_url", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if name == "is_active":
                field.widget.attrs.setdefault("class", "oh-switch__checkbox")
            else:
                field.widget.attrs.setdefault("class", "oh-input w-100")
        if self.instance.pk:
            keep = _("Leave blank to keep the current value.")
            self.fields["access_token"].help_text = keep
            self.fields["channel_secret"].help_text = keep

    def clean_access_token(self):
        value = self.cleaned_data.get("access_token", "")
        if not value and not self.instance.access_token_encrypted:
            raise forms.ValidationError(_("This field is required."))
        if value and (len(value) > 1000 or any(c.isspace() for c in value)):
            raise forms.ValidationError(_("This does not look like a channel access token."))
        return value

    def clean_channel_secret(self):
        value = self.cleaned_data.get("channel_secret", "")
        if not value and not self.instance.channel_secret_encrypted:
            raise forms.ValidationError(_("This field is required."))
        if value and (len(value) > 200 or any(c.isspace() for c in value)):
            raise forms.ValidationError(_("This does not look like a channel secret."))
        return value

    def clean_company(self):
        company = self.cleaned_data.get("company")
        if company is None:
            others = LineChannel.all_objects.filter(company__isnull=True).exclude(pk=self.instance.pk)
            if others.exists():
                raise forms.ValidationError(
                    _("A default channel (no company) already exists.")
                )
        return company

    def save(self, commit=True):
        channel = super().save(commit=False)
        if self.cleaned_data.get("access_token"):
            channel.access_token = self.cleaned_data["access_token"]
        if self.cleaned_data.get("channel_secret"):
            channel.channel_secret = self.cleaned_data["channel_secret"]
        if commit:
            channel.save()
        return channel
