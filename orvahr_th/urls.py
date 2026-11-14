from django.urls import path

from orvahr_th import certificate_views, views

urlpatterns = [
    # Employee database id and year only: the tax ID never goes in a URL.
    path(
        "wht-certificate/<int:employee_id>/<int:year>/",
        views.wht_certificate,
        name="orvahr-th-wht-certificate",
    ),
    path(
        "wht-certificate/<int:employee_id>/<int:year>/pdf/",
        views.wht_certificate_pdf,
        name="orvahr-th-wht-certificate-pdf",
    ),
    # Employment / salary certificates: addressed by a random token only.
    path(
        "certificates/",
        certificate_views.certificate_list,
        name="orvahr-th-certificates",
    ),
    path(
        "certificates/request/",
        certificate_views.certificate_request,
        name="orvahr-th-certificate-request",
    ),
    path(
        "certificates/generate/",
        certificate_views.certificate_generate,
        name="orvahr-th-certificate-generate",
    ),
    path(
        "certificates/<uuid:token>/approve/",
        certificate_views.certificate_approve,
        name="orvahr-th-certificate-approve",
    ),
    path(
        "certificates/<uuid:token>/reject/",
        certificate_views.certificate_reject,
        name="orvahr-th-certificate-reject",
    ),
    path(
        "certificates/<uuid:token>/pdf/",
        certificate_views.certificate_pdf,
        name="orvahr-th-certificate-pdf",
    ),
]
