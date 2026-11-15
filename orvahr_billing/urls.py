"""Billing URLs, mounted under /billing/ on the public (control-plane) host."""

from django.urls import path

from orvahr_billing import views

app_name = "orvahr_billing"

urlpatterns = [
    path("", views.document_list, name="document-list"),
    path("documents/<int:pk>/", views.document_detail, name="document-detail"),
    path("documents/<int:pk>/issue/", views.document_issue, name="document-issue"),
    path("documents/<int:pk>/delete/", views.document_delete, name="document-delete"),
    path("documents/<int:pk>/pdf/", views.document_pdf, name="document-pdf"),
    path("documents/<int:pk>/email/", views.document_email, name="document-email"),
    path("documents/<int:pk>/payment/", views.payment_create, name="payment-create"),
    path(
        "documents/<int:pk>/credit-note/",
        views.credit_note_create,
        name="credit-note-create",
    ),
    path("invoices/new/", views.invoice_create, name="invoice-create"),
    path("payments/<int:pk>/slip/", views.payment_slip, name="payment-slip"),
    path("settings/", views.seller_settings, name="seller-settings"),
    path("tenants/", views.tenant_list, name="tenant-list"),
    path("tenants/<int:pk>/", views.tenant_billing, name="tenant-billing"),
    path("prices/", views.price_list, name="price-list"),
    path("notices/", views.notice_list, name="notice-list"),
    path(
        "notices/<int:tenant_pk>/<uuid:token>/slip/",
        views.notice_slip,
        name="notice-slip",
    ),
    path(
        "notices/<int:tenant_pk>/<uuid:token>/confirm/",
        views.notice_confirm,
        name="notice-confirm",
    ),
    path(
        "notices/<int:tenant_pk>/<uuid:token>/reject/",
        views.notice_reject,
        name="notice-reject",
    ),
]
