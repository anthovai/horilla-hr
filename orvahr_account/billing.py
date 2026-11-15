"""
Read-only access to the control plane (orvahr_saas / orvahr_billing, public
schema) from a TENANT request.

The tenant connection's search_path is "<tenant>, public", so the public
billing tables are readable -- and so are every other customer's rows. Every
query here is therefore filtered by the tenant the connection is bound to
(``connection.tenant``, set by the tenant middleware from the request host),
never by anything the browser sent. Drafts are never returned.

orvahr_billing / orvahr_saas are installed only in SaaS mode, so their models
are imported inside the functions.
"""

from __future__ import annotations

import datetime

from django.db import connection
from django.http import Http404
from django.utils import timezone

from horilla.tenancy import is_multi_tenant, public_schema_name


def bound_tenant():
    """
    The orvahr_saas.Tenant the connection is bound to, or Http404.

    A FakeTenant (``schema_context``) or the public schema is refused: the
    pages exist only for a real customer reached through its own host.
    """
    if not is_multi_tenant():
        raise Http404
    from orvahr_saas.models import Tenant

    tenant = getattr(connection, "tenant", None)
    if (
        not isinstance(tenant, Tenant)
        or tenant.pk is None
        or tenant.schema_name == public_schema_name()
        or connection.schema_name != tenant.schema_name
    ):
        raise Http404
    return tenant


# -------------------------------------------------------------- documents


def issued_documents(tenant):
    """This tenant's issued invoices, tax invoices/receipts and credit notes."""
    from orvahr_billing.models import BillingDocument, DocStatus

    return BillingDocument.objects.filter(
        tenant_id=tenant.pk, status=DocStatus.ISSUED, number__isnull=False
    ).order_by("-issue_date", "-pk")


def get_issued_document(tenant, number):
    """One issued document of ``tenant`` by its public number, else Http404."""
    doc = issued_documents(tenant).filter(number=number).first()
    if doc is None:
        raise Http404
    return doc


def open_invoices(tenant):
    """Issued invoices of ``tenant`` with money still outstanding."""
    from orvahr_billing.models import DocType, PaymentStatus

    return (
        issued_documents(tenant)
        .filter(
            doc_type=DocType.INVOICE,
            payment_status__in=[PaymentStatus.UNPAID, PaymentStatus.PARTIAL],
        )
        .order_by("issue_date", "pk")
    )


def overdue_invoices(tenant, today=None):
    from django.db.models import Q

    today = today or timezone.localdate()
    return open_invoices(tenant).filter(
        Q(overdue_since__isnull=False) | Q(due_date__lt=today)
    )


# ---------------------------------------------------------- plan / profile


def subscription(tenant):
    from orvahr_billing.models import Subscription

    return Subscription.objects.filter(tenant_id=tenant.pk).first()


def billing_profile(tenant):
    from orvahr_billing.models import BillingProfile

    return BillingProfile.objects.filter(tenant_id=tenant.pk).first()


def public_plans():
    from orvahr_saas.models import Plan

    return Plan.objects.filter(is_public=True).order_by("sort", "code")


def account_status(tenant, today=None):
    """
    ``{"code": "trial"|"active"|"overdue"|"suspended", "days_left": n|None,
    "trial_ends_on": date|None, "overdue": [invoices]}``.
    """
    today = today or timezone.localdate()
    overdue = list(overdue_invoices(tenant, today))
    days_left = None
    trial_ends_on = getattr(tenant, "trial_ends_on", None)
    if tenant.status == tenant.STATUS_TRIAL and isinstance(
        trial_ends_on, datetime.date
    ):
        days_left = max((trial_ends_on - today).days, 0)
    if overdue:
        code = "overdue"
    else:
        code = tenant.status
    return {
        "code": code,
        "tenant_status": tenant.status,
        "days_left": days_left,
        "trial_ends_on": trial_ends_on,
        "overdue": overdue,
    }
