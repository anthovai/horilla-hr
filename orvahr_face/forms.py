from django import forms

from orvahr_face.models import FaceSettings


class FaceSettingsForm(forms.ModelForm):
    class Meta:
        model = FaceSettings
        fields = ["enabled", "required_for_web", "liveness_required", "match_threshold", "retention_days"]
        widgets = {"match_threshold": forms.NumberInput(attrs={"step": "0.01", "min": "0.30", "max": "0.99"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "oh-switch__checkbox")
            else:
                widget.attrs.setdefault("class", "oh-input w-100")
