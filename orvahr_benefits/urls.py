"""URLs for orvahr_benefits (mounted at th-benefits/). Database ids only."""

from django.urls import path

from orvahr_benefits import views

app_name = "orvahr_benefits"

urlpatterns = [
    path("", views.home, name="home"),
    # provident fund
    path("pvd/plans/", views.plan_list, name="pvd-plans"),
    path("pvd/plans/new/", views.plan_create, name="pvd-plan-create"),
    path("pvd/plans/<int:pk>/edit/", views.plan_update, name="pvd-plan-update"),
    path("pvd/members/", views.member_list, name="pvd-members"),
    path("pvd/members/new/", views.member_create, name="pvd-member-create"),
    path("pvd/members/<int:pk>/edit/", views.member_update, name="pvd-member-update"),
    path("pvd/sync/", views.pvd_sync, name="pvd-sync"),
    path("pvd/tax-claims/", views.pvd_tax_claims, name="pvd-tax-claims"),
    path("pvd/report/", views.pvd_report, name="pvd-report"),
    path("pvd/report/csv/", views.pvd_report_csv, name="pvd-report-csv"),
    # severance and final settlement
    path("severance-rules/", views.severance_rules, name="severance-rules"),
    path("settlements/", views.settlement_list, name="settlements"),
    path("settlements/new/", views.settlement_create, name="settlement-create"),
    path("settlements/<int:pk>/", views.settlement_detail, name="settlement-detail"),
    path("settlements/<int:pk>/edit/", views.settlement_update, name="settlement-update"),
    path("settlements/<int:pk>/pdf/", views.settlement_pdf, name="settlement-pdf"),
    path(
        "settlements/<int:pk>/recompute/",
        views.settlement_recompute,
        name="settlement-recompute",
    ),
    path(
        "settlements/<int:pk>/approve/", views.settlement_approve, name="settlement-approve"
    ),
    path(
        "settlements/<int:pk>/paid/", views.settlement_mark_paid, name="settlement-paid"
    ),
    # deduction orders
    path("orders/", views.order_list, name="orders"),
    path("orders/new/", views.order_create, name="order-create"),
    path("orders/<int:pk>/edit/", views.order_update, name="order-update"),
    path("orders/sync/", views.orders_sync, name="orders-sync"),
    path("orders/remittance/", views.remittance_report, name="remittance"),
    path("orders/remittance/csv/", views.remittance_csv, name="remittance-csv"),
    # self-service
    path("my/", views.my_benefits, name="my-benefits"),
]
