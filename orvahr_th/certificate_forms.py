from django import forms
from django.utils.translation import gettext_lazy as _

from employee.models import Employee
from orvahr_th.models import EmploymentCertificate


class _Styled:
    def _style(self):
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "oh-input w-100")


class CertificateRequestForm(_Styled, forms.ModelForm):
    class Meta:
        model = EmploymentCertificate
        fields = ["kind", "language", "purpose"]
        help_texts = {
            "purpose": _("e.g. bank loan application, visa application"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


class CertificateSignerForm(_Styled, forms.Form):
    signer_name = forms.CharField(label=_("Signer name"), max_length=150)
    signer_title = forms.CharField(label=_("Signer title"), max_length=150)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()


class CertificateGenerateForm(_Styled, forms.ModelForm):
    signer_name = forms.CharField(label=_("Signer name"), max_length=150)
    signer_title = forms.CharField(label=_("Signer title"), max_length=150)

    class Meta:
        model = EmploymentCertificate
        fields = ["employee", "kind", "language", "purpose"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["employee"].queryset = Employee.objects.filter(is_active=True)
        self._style()


class CertificateRejectForm(_Styled, forms.Form):
    reason = forms.CharField(label=_("Reason"), max_length=255)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._style()
