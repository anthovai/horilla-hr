"""
Tenant-side account URLs, mounted at /subscription/ by
``OrvahrAccountConfig.ready()`` in SaaS mode only.

Documents are addressed by their public number, notices / requests by a
random token -- see the module docstring of ``orvahr_account.views``.
"""

from django.urls import path

from orvahr_account import views

app_name = "orvahr_account"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("documents/", views.documents, name="documents"),
    path("documents/<slug:number>/pdf/", views.document_pdf, name="document-pdf"),
    path("payments/notify/", views.payment_notify, name="payment-notify"),
    path(
        "payments/<uuid:token>/slip/",
        views.payment_notice_slip,
        name="payment-notice-slip",
    ),
    path("plan/", views.plan_change, name="plan-change"),
    path(
        "plan/<uuid:token>/cancel/",
        views.plan_change_cancel,
        name="plan-change-cancel",
    ),
]
