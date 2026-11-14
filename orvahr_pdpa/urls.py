"""URLs for orvahr_pdpa (mounted at /pdpa/ by apps.py)."""

from django.urls import path

from orvahr_pdpa import views

app_name = "orvahr_pdpa"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("my-privacy/", views.my_privacy, name="my-privacy"),
    # Settings
    path("settings/", views.settings_view, name="settings"),
    path("settings/dpo/create/", views.settings_form, name="settings-create"),
    path("settings/dpo/<int:pk>/", views.settings_form, name="settings-update"),
    path("settings/notice/create/", views.notice_form, name="notice-create"),
    path("settings/notice/<int:pk>/", views.notice_form, name="notice-update"),
    path("settings/notice/<int:pk>/view/", views.notice_detail, name="notice-detail"),
    path("settings/purpose/create/", views.purpose_form, name="purpose-create"),
    path("settings/purpose/<int:pk>/", views.purpose_form, name="purpose-update"),
    path("settings/retention/create/", views.policy_form, name="policy-create"),
    path("settings/retention/<int:pk>/", views.policy_form, name="policy-update"),
    # Consent
    path("consents/", views.consent_status, name="consent-status"),
    # Data subject requests
    path("requests/", views.dsr_list, name="dsr-list"),
    path("requests/create/", views.dsr_create, name="dsr-create"),
    path("requests/<int:pk>/", views.dsr_detail, name="dsr-detail"),
    path("export/<int:employee_id>/", views.export_employee, name="export-employee"),
    # Retention
    path("retention/", views.retention_report, name="retention-report"),
    path("retention/<int:pk>/", views.retention_policy_detail, name="retention-policy"),
    path("retention/<int:pk>/anonymise/", views.retention_anonymise, name="retention-anonymise"),
    # Access log
    path("access-log/", views.access_log, name="access-log"),
    # Breaches
    path("breaches/", views.breach_list, name="breach-list"),
    path("breaches/create/", views.breach_form, name="breach-create"),
    path("breaches/<int:pk>/", views.breach_form, name="breach-update"),
]
