"""
Employment / salary certificate requests.

Who may do what:
* any employee: request a certificate for themselves, see their own
  requests, download their own once approved;
* orvahr_th.view_employmentcertificate: see every request and download any
  approved certificate;
* orvahr_th.change_employmentcertificate: approve (entering the signer) or
  reject a pending request -- not their own, unless superuser;
* orvahr_th.add_employmentcertificate: generate a certificate directly.

Certificates are addressed by their random ``token``; no database or
employee id appears in a URL.
"""

import contextlib
import logging

from django.apps import apps
from django.contrib import messages
from django.http import Http404, HttpResponse, HttpResponseForbidden
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop
from django.views.decorators.http import require_http_methods

from horilla.decorators import login_required, permission_required
from orvahr_th import certificates
from orvahr_th.certificate_forms import (
    CertificateGenerateForm,
    CertificateRejectForm,
    CertificateRequestForm,
    CertificateSignerForm,
)
from orvahr_th.models import EmploymentCertificate

logger = logging.getLogger(__name__)

VIEW_PERM = "orvahr_th.view_employmentcertificate"
CHANGE_PERM = "orvahr_th.change_employmentcertificate"
ADD_PERM = "orvahr_th.add_employmentcertificate"


def _employee(request):
    return getattr(request.user, "employee_get", None)


def _get(token):
    cert = (
        EmploymentCertificate.all_objects.select_related(
            "employee", "employee__employee_work_info", "company"
        )
        .filter(token=token)
        .first()
    )
    if cert is None:
        raise Http404
    return cert


def _is_owner(request, cert):
    employee = _employee(request)
    return employee is not None and cert.employee_id == employee.pk


def _hr_queryset(request):
    # objects follows the company selected in the session.
    return EmploymentCertificate.objects.select_related("employee", "company")


def _no_store(response):
    response["Cache-Control"] = "no-store, private"
    return response


def _notify(cert):
    """In-app and LINE notice to the employee that a decision was made."""
    approved = cert.status == EmploymentCertificate.STATUS_APPROVED
    user = cert.employee.employee_user_id
    if user is not None:
        with contextlib.suppress(Exception):
            from notifications.signals import notify

            notify.send(
                cert.decided_by or cert.employee,
                recipient=user,
                verb=(
                    gettext_noop("Your certificate request has been approved.")
                    if approved
                    else gettext_noop("Your certificate request has been rejected.")
                ),
                icon="document-text-outline",
                redirect=reverse("orvahr-th-certificates"),
            )
    if apps.is_installed("line_integration"):
        with contextlib.suppress(Exception):
            from line_integration import dispatch

            dispatch.queue(_line_push, cert.pk)


def _line_push(cert_pk):
    """Runs after commit, off the request thread (line_integration.dispatch)."""
    from line_integration import dispatch

    cert = (
        EmploymentCertificate.all_objects.select_related("employee")
        .filter(pk=cert_pk)
        .first()
    )
    if cert is None:
        return
    if cert.status == EmploymentCertificate.STATUS_APPROVED:
        text = _("Your %(certificate)s is ready to download in OrvaHR.")
    elif cert.status == EmploymentCertificate.STATUS_REJECTED:
        text = _("Your %(certificate)s request was rejected.")
    else:
        return
    text = text % {"certificate": cert.get_kind_display().lower()}
    link = getattr(cert.employee, "line_link", None)
    channel = getattr(link, "channel", None)
    if channel is not None:
        with contextlib.suppress(Exception):
            url = channel.absolute_url(reverse("orvahr-th-certificates"))
            if url:
                text += "\n" + url
    dispatch.notify_employee(cert.employee, text, event=f"certificate.{cert.status}")


@login_required
def certificate_list(request):
    employee = _employee(request)
    own = (
        EmploymentCertificate.all_objects.filter(employee=employee)
        if employee is not None
        else EmploymentCertificate.all_objects.none()
    )
    hr_rows = None
    if request.user.has_perm(VIEW_PERM):
        hr_rows = _hr_queryset(request)[:200]
    return _no_store(
        render(
            request,
            "orvahr_th/certificate_list.html",
            {
                "own": own,
                "hr_rows": hr_rows,
                "can_decide": request.user.has_perm(CHANGE_PERM),
                "can_generate": request.user.has_perm(ADD_PERM),
            },
        )
    )


@login_required
def certificate_request(request):
    employee = _employee(request)
    if employee is None:
        return HttpResponseForbidden()
    form = CertificateRequestForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        cert = form.save(commit=False)
        cert.employee = employee
        cert.requested_by = employee
        cert.company = certificates.company_for(employee)
        cert.save()
        messages.success(request, _("Certificate requested."))
        return redirect("orvahr-th-certificates")
    return render(
        request,
        "orvahr_th/certificate_form.html",
        {"form": form, "title": _("Request a certificate")},
    )


@login_required
@permission_required(ADD_PERM)
def certificate_generate(request):
    form = CertificateGenerateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        cert = form.save(commit=False)
        cert.requested_by = _employee(request)
        cert.company = certificates.company_for(cert.employee)
        try:
            certificates.issue(
                cert,
                signer_name=form.cleaned_data["signer_name"],
                signer_title=form.cleaned_data["signer_title"],
                decided_by=_employee(request),
            )
        except certificates.CertificateError:
            messages.error(
                request,
                _("The employee has no active contract with a wage."),
            )
        else:
            messages.success(request, _("Certificate issued."))
            return redirect("orvahr-th-certificates")
    return render(
        request,
        "orvahr_th/certificate_form.html",
        {"form": form, "title": _("Generate a certificate")},
    )


def _decision_denied(request, cert):
    if not request.user.has_perm(CHANGE_PERM):
        return HttpResponseForbidden()
    if not request.user.is_superuser and _is_owner(request, cert):
        return HttpResponseForbidden()
    return None


@login_required
def certificate_approve(request, token):
    cert = _get(token)
    denied = _decision_denied(request, cert)
    if denied:
        return denied
    if cert.status != EmploymentCertificate.STATUS_REQUESTED:
        messages.error(request, _("This request has already been decided."))
        return redirect("orvahr-th-certificates")
    form = CertificateSignerForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            cert = certificates.issue(
                cert,
                signer_name=form.cleaned_data["signer_name"],
                signer_title=form.cleaned_data["signer_title"],
                decided_by=_employee(request),
            )
        except certificates.CertificateError:
            messages.error(
                request,
                _(
                    "The certificate could not be issued: it was already decided, "
                    "or the employee has no active contract with a wage."
                ),
            )
        else:
            _notify(cert)
            messages.success(request, _("Certificate approved."))
            return redirect("orvahr-th-certificates")
    return render(
        request,
        "orvahr_th/certificate_form.html",
        {"form": form, "title": _("Approve certificate request"), "cert": cert},
    )


@login_required
def certificate_reject(request, token):
    cert = _get(token)
    denied = _decision_denied(request, cert)
    if denied:
        return denied
    form = CertificateRejectForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            cert = certificates.reject(
                cert, reason=form.cleaned_data["reason"], decided_by=_employee(request)
            )
        except certificates.CertificateError:
            messages.error(request, _("This request has already been decided."))
        else:
            _notify(cert)
            messages.success(request, _("Certificate request rejected."))
        return redirect("orvahr-th-certificates")
    return render(
        request,
        "orvahr_th/certificate_form.html",
        {"form": form, "title": _("Reject certificate request"), "cert": cert},
    )


@login_required
@require_http_methods(["GET"])
def certificate_pdf(request, token):
    cert = _get(token)
    if not (_is_owner(request, cert) or request.user.has_perm(VIEW_PERM)):
        return HttpResponseForbidden()
    if not cert.is_issued:
        # Nothing to download until HR has approved it.
        raise Http404
    data = certificates.render_pdf(cert, request=request)
    response = HttpResponse(data, content_type="application/pdf")
    response["Content-Disposition"] = (
        f'inline; filename="{certificates.pdf_filename(cert)}"'
    )
    return _no_store(response)
