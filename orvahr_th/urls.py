from django.urls import path

from orvahr_th import views

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
]
