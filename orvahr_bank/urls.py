"""URLs for orvahr_bank (mounted at th-bank/). No personal IDs in any URL."""

from django.urls import path

from orvahr_bank import views

app_name = "orvahr_bank"

urlpatterns = [
    path("export/", views.bank_export, name="export"),
    path("accounts/", views.account_list, name="accounts"),
    path("accounts/new/", views.account_create, name="account-create"),
    path("accounts/<int:pk>/", views.account_update, name="account-update"),
]
