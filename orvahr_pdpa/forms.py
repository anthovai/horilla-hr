from django import forms
from django.utils.translation import gettext_lazy as _

from orvahr_pdpa.models import (
    BreachIncident,
    ConsentPurpose,
    DataSubjectRequest,
    PdpaSettings,
    PrivacyNotice,
    RetentionPolicy,
)

DATETIME_FORMAT = "%Y-%m-%dT%H:%M"


class DateTimeLocalInput(forms.DateTimeInput):
    input_type = "datetime-local"

    def __init__(self, attrs=None):
        super().__init__(attrs=attrs, format=DATETIME_FORMAT)


class StyledModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "oh-switch__checkbox")
            elif isinstance(widget, forms.Select):
                widget.attrs.setdefault("class", "oh-select w-100")
            else:
                widget.attrs.setdefault("class", "oh-input w-100")
            if isinstance(widget, forms.DateTimeInput):
                field.input_formats = [DATETIME_FORMAT, "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"]


class PdpaSettingsForm(StyledModelForm):
    class Meta:
        model = PdpaSettings
        fields = ["company", "dpo_name", "dpo_email", "dpo_phone", "dpo_address", "dsr_due_days", "breach_deadline_hours"]
        widgets = {"dpo_address": forms.Textarea(attrs={"rows": 3})}


class PrivacyNoticeForm(StyledModelForm):
    class Meta:
        model = PrivacyNotice
        fields = ["company", "audience", "version", "title", "effective_from", "text_th", "text_en", "published", "is_active"]
        widgets = {
            "effective_from": forms.DateInput(attrs={"type": "date"}),
            "text_th": forms.Textarea(attrs={"rows": 10}),
            "text_en": forms.Textarea(attrs={"rows": 10}),
        }
        help_texts = {"text_th": _("Legal content: have counsel review before publishing.")}

    def clean(self):
        cleaned = super().clean()
        # The unique constraint includes a nullable company, which databases
        # do not enforce for NULL; check it here.
        qs = PrivacyNotice.all_objects.filter(
            company=cleaned.get("company"), audience=cleaned.get("audience"), version=cleaned.get("version")
        ).exclude(pk=self.instance.pk)
        if cleaned.get("version") and qs.exists():
            raise forms.ValidationError(_("This version already exists for this audience."))
        if self.instance.pk and self.instance.consents.exists():
            changed = {"text_th", "text_en", "version", "audience"} & set(self.changed_data)
            if changed:
                raise forms.ValidationError(
                    _("People have already responded to this notice. Create a new version instead of editing its text.")
                )
        return cleaned


class ConsentPurposeForm(StyledModelForm):
    class Meta:
        model = ConsentPurpose
        fields = ["company", "audience", "code", "name", "name_th", "description", "optional", "is_active"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}


class RetentionPolicyForm(StyledModelForm):
    class Meta:
        model = RetentionPolicy
        fields = ["company", "name", "model_label", "date_field", "keep_years", "action", "notes", "is_active"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}


class DataSubjectRequestForm(StyledModelForm):
    class Meta:
        model = DataSubjectRequest
        fields = [
            "employee", "candidate", "subject_name", "request_type", "received_at", "due_at",
            "assignee", "details", "notes",
        ]
        widgets = {
            "received_at": DateTimeLocalInput(),
            "due_at": DateTimeLocalInput(),
            "details": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["due_at"].required = False


class DsrUpdateForm(StyledModelForm):
    status = forms.ChoiceField(label=_("Status"))

    class Meta:
        model = DataSubjectRequest
        fields = ["status", "assignee", "due_at", "notes", "resolution"]
        widgets = {
            "due_at": DateTimeLocalInput(),
            "notes": forms.Textarea(attrs={"rows": 3}),
            "resolution": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current = self.instance.status
        allowed = (current,) + tuple(self.instance.allowed_next())
        labels = dict(DataSubjectRequest.STATUS_CHOICES)
        self.fields["status"].choices = [(s, labels[s]) for s in allowed]
        self.fields["status"].widget.attrs["class"] = "oh-select w-100"

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("status") in ("completed", "rejected") and not cleaned.get("resolution"):
            self.add_error("resolution", _("Describe the outcome before closing the request."))
        return cleaned


class BreachIncidentForm(StyledModelForm):
    class Meta:
        model = BreachIncident
        fields = [
            "company", "title", "description", "detected_at", "data_categories", "affected_subjects_count",
            "risk_level", "deadline_at", "reported_to_pdpc_at", "subjects_notified_at", "actions_taken", "status",
        ]
        widgets = {
            "detected_at": DateTimeLocalInput(),
            "deadline_at": DateTimeLocalInput(),
            "reported_to_pdpc_at": DateTimeLocalInput(),
            "subjects_notified_at": DateTimeLocalInput(),
            "description": forms.Textarea(attrs={"rows": 4}),
            "actions_taken": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["deadline_at"].required = False


class AnonymiseConfirmForm(forms.Form):
    CONFIRM_WORD = "ANONYMISE"

    confirm = forms.CharField(
        label=_("Type ANONYMISE to confirm"),
        widget=forms.TextInput(attrs={"class": "oh-input w-100", "autocomplete": "off"}),
    )
    record_ids = forms.CharField(widget=forms.HiddenInput)

    def clean_confirm(self):
        value = self.cleaned_data["confirm"].strip()
        if value != self.CONFIRM_WORD:
            raise forms.ValidationError(_("Confirmation text does not match."))
        return value

    def clean_record_ids(self):
        try:
            ids = [int(v) for v in self.cleaned_data["record_ids"].split(",") if v.strip()]
        except ValueError:
            raise forms.ValidationError(_("Invalid selection."))
        if not ids:
            raise forms.ValidationError(_("Select at least one record."))
        return ids
