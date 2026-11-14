"""URLs for orvahr_filing. Company and period go in the query string; no
personal ID (tax ID, national ID, SSO number) is ever part of a URL."""

from django.urls import path

from orvahr_filing import views

app_name = "orvahr_filing"

urlpatterns = [
    path("", views.filing_home, name="home"),
    path("settings/company-tax-profile/", views.tax_profile_settings, name="tax-profile"),
    path("pnd1/", views.pnd1_monthly, name="pnd1"),
    path("pnd1/attachment.pdf", views.pnd1_monthly_pdf, name="pnd1-pdf"),
    path("pnd1/rd-file.txt", views.pnd1_monthly_rd, name="pnd1-rd"),
    path("pnd1k/rd-file.txt", views.pnd1_annual_rd, name="pnd1k-rd"),
    path("pnd1k/", views.pnd1_annual, name="pnd1k"),
    path("pnd1k/attachment.pdf", views.pnd1_annual_pdf, name="pnd1k-pdf"),
    path("sso/", views.sso_monthly, name="sso"),
    path("sso/contributions.csv", views.sso_csv, name="sso-csv"),
    path("sso/joiners-leavers.csv", views.sso_movements_csv, name="sso-movements-csv"),
]
