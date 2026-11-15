"""
Control-plane e-mails (public schema), TH/EN.

=====================  ==============================  =========================
event                  to                              sent by
=====================  ==============================  =========================
request_received       operators                       public sign-up form
tenant_provisioned     applicant (URL + first-login    provisioning (never the
                       instructions)                   password)
trial_ending           tenant contact + operators      ``saas_lifecycle``
trial_ended            tenant contact + operators      ``saas_lifecycle``
invoice_issued         invoice e-mail of the customer  orvahr_billing (hook)
payment_confirmed      invoice e-mail of the customer  orvahr_billing (hook)
tenant_suspended       tenant contact                  ``services.suspend_tenant``
tenant_resumed         tenant contact                  ``services.resume_tenant``
=====================  ==============================  =========================

Mail goes out through ``public_email_connection()`` -- never Horilla's
``ConfiguredEmailBackend``, which reads the mail server from tenant tables
(see ``orvahr_saas.conf.email_backend``). Every message is sent after the
surrounding transaction commits, one recipient per message, and logged in
``NotificationLog`` (event, recipient address, outcome -- no subject, no
body). A failure is logged and never propagates to the caller. With the flag
off every function here is a no-op.

Templates: ``orvahr_saas/email/<event>.<th|en>.txt`` -- first line is the
subject, the rest (after one blank line) the body.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import translation

from horilla.tenancy import is_multi_tenant, public_schema_name, use_schema
from orvahr_saas import conf

logger = logging.getLogger(__name__)

AUDIENCE_OPERATOR = "operator"
AUDIENCE_TENANT = "tenant"
AUDIENCE_APPLICANT = "applicant"

EVENTS = (
    "request_received",
    "tenant_provisioned",
    "trial_ending",
    "trial_ended",
    "invoice_issued",
    "payment_confirmed",
    "tenant_suspended",
    "tenant_resumed",
)


# -------------------------------------------------------------- sending


def public_email_connection():
    """Mail connection that works on the public schema (no tenant tables)."""
    from django.core.mail import get_connection

    return get_connection(conf.email_backend())


def build_public_message(subject, body, to, *, from_email=None, connection=None):
    """
    ``EmailMessage`` built with Django's own initialiser: ``base.backends``
    patches ``EmailMessage.__init__`` to read the tenant's mail settings,
    which the public schema does not have.
    """
    from django.core.mail import EmailMessage

    try:
        from base import backends as horilla_backends

        original_init = getattr(horilla_backends, "message_init", None)
    except Exception:  # noqa: BLE001 - base not importable: nothing patched
        original_init = None
    message = EmailMessage.__new__(EmailMessage)
    (original_init or EmailMessage.__init__)(
        message,
        subject=subject,
        body=body,
        from_email=from_email or conf.from_email(),
        to=list(to),
        connection=connection or public_email_connection(),
    )
    return message


def _lang(language):
    return "th" if (language or "").lower().startswith("th") else "en"


def render(event, language, context):
    """``(subject, body)`` of ``event`` in ``language`` (th / en)."""
    lang = _lang(language)
    ctx = {"support_email": conf.support_email(), **context}
    with translation.override(lang):
        text = render_to_string(f"orvahr_saas/email/{event}.{lang}.txt", ctx)
    subject, _sep, body = text.strip().partition("\n")
    return " ".join(subject.split()), body.strip() + "\n"


def _clean(addresses):
    seen, out = set(), []
    for address in addresses:
        address = (address or "").strip()
        if address and "@" in address and address.lower() not in seen:
            seen.add(address.lower())
            out.append(address)
    return out


def send(
    event,
    *,
    to,
    audience,
    context,
    language,
    tenant=None,
    tenant_request=None,
    object_ref="",
    dedupe_key="",
):
    """
    Queue ``event`` for every address in ``to`` (sent after commit).
    Returns the list of addresses queued.
    """
    if not is_multi_tenant() or not conf.notifications_enabled():
        return []
    if event in conf.disabled_events():
        return []
    recipients = _clean(to)
    if not recipients:
        return []
    tenant_pk = getattr(tenant, "pk", None)
    request_pk = getattr(tenant_request, "pk", None)

    def deliver():
        for address in recipients:
            _deliver_one(
                event,
                address,
                audience=audience,
                context=context,
                language=language,
                tenant_pk=tenant_pk,
                request_pk=request_pk,
                object_ref=object_ref,
                dedupe_key=dedupe_key,
            )

    transaction.on_commit(deliver)
    return recipients


def _deliver_one(
    event,
    address,
    *,
    audience,
    context,
    language,
    tenant_pk,
    request_pk,
    object_ref,
    dedupe_key,
):
    from orvahr_saas.models import NotificationLog

    with use_schema(public_schema_name()):
        if (
            dedupe_key
            and NotificationLog.objects.filter(
                dedupe_key=dedupe_key,
                recipient__iexact=address,
                status=NotificationLog.STATUS_SENT,
            ).exists()
        ):
            return None
        status, error = NotificationLog.STATUS_SENT, ""
        try:
            subject, body = render(event, language, context)
            build_public_message(subject, body, [address]).send()
        except Exception as exc:  # noqa: BLE001 - mail must never break callers
            logger.exception("Notification %s to a recipient failed", event)
            status = NotificationLog.STATUS_FAILED
            error = f"{type(exc).__name__}: {exc}"[:255]
        return NotificationLog.objects.create(
            event=event,
            recipient=address[:254],
            audience=audience,
            language=_lang(language),
            tenant_id=tenant_pk,
            tenant_request_id=request_pk,
            object_ref=object_ref[:100],
            dedupe_key=dedupe_key[:150],
            status=status,
            error=error,
        )


# ---------------------------------------------------------- recipients


def operator_emails():
    from orvahr_saas.models import ConsoleOperator

    with use_schema(public_schema_name()):
        emails = list(
            ConsoleOperator.objects.filter(is_active=True, is_staff=True)
            .exclude(email="")
            .values_list("email", flat=True)
        )
    return _clean(emails + conf.extra_operator_emails())


def _billing_email(tenant):
    try:
        profile = tenant.billing_profile
    except Exception:  # noqa: BLE001 - no profile / billing not installed
        return ""
    return getattr(profile, "email", "") or ""


def tenant_emails(tenant):
    """The customer's contact address (tenant row, else billing profile)."""
    return _clean([tenant.contact_email or _billing_email(tenant)])


def primary_domain(tenant):
    domain = tenant.domains.filter(is_primary=True).first() or tenant.domains.first()
    return domain.domain if domain else ""


def tenant_url(tenant, path="/"):
    domain = primary_domain(tenant)
    if not domain:
        return ""
    return f"{conf.url_scheme()}://{domain}{path}"


def console_url(path="/console/"):
    base = getattr(settings, "ORVAHR_SAAS_BASE_DOMAIN", "")
    return f"{conf.url_scheme()}://{base}{path}" if base else path


def _tenant_context(tenant, **extra):
    return {
        "tenant_name": tenant.name,
        "login_url": tenant_url(tenant, "/login/"),
        "account_url": tenant_url(tenant, "/subscription/"),
        **extra,
    }


# ------------------------------------------------------------- triggers


def request_received(tenant_request):
    """New sign-up request -> operators (no contact details in the mail)."""
    plan = tenant_request.plan
    return send(
        "request_received",
        to=operator_emails(),
        audience=AUDIENCE_OPERATOR,
        language=conf.operator_language(),
        tenant_request=tenant_request,
        object_ref=f"request:{tenant_request.pk}",
        dedupe_key=f"request_received:{tenant_request.pk}",
        context={
            "company_name": tenant_request.company_name,
            "slug": tenant_request.slug,
            "employee_count": tenant_request.employee_count,
            "plan_name": plan.name_en if plan else "",
            "review_url": console_url(f"/console/requests/{tenant_request.pk}/"),
        },
    )


def tenant_provisioned(tenant_request, tenant):
    """Workspace ready -> applicant: URL + how to sign in. Never the password."""
    return send(
        "tenant_provisioned",
        to=[tenant_request.contact_email],
        audience=AUDIENCE_APPLICANT,
        language=tenant_request.language or conf.customer_language(),
        tenant=tenant,
        tenant_request=tenant_request,
        object_ref=f"request:{tenant_request.pk}",
        dedupe_key=f"tenant_provisioned:{tenant.pk}",
        context=_tenant_context(
            tenant,
            contact_name=tenant_request.contact_name,
            username=tenant_request.contact_email,
            trial_ends_on=tenant.trial_ends_on,
            is_trial=tenant.status == tenant.STATUS_TRIAL,
        ),
    )


def trial_ending(tenant, days_left, threshold):
    context = _tenant_context(
        tenant, days_left=days_left, trial_ends_on=tenant.trial_ends_on
    )
    key = f"trial_ending:{tenant.pk}:{tenant.trial_ends_on}:{threshold}"
    sent = send(
        "trial_ending",
        to=tenant_emails(tenant),
        audience=AUDIENCE_TENANT,
        language=conf.customer_language(),
        tenant=tenant,
        object_ref=f"tenant:{tenant.pk}",
        dedupe_key=key,
        context=context,
    )
    sent += send(
        "trial_ending",
        to=operator_emails(),
        audience=AUDIENCE_OPERATOR,
        language=conf.operator_language(),
        tenant=tenant,
        object_ref=f"tenant:{tenant.pk}",
        dedupe_key=key,
        context={**context, "for_operator": True, "schema": tenant.schema_name},
    )
    return sent


def trial_ended(tenant):
    context = _tenant_context(tenant, trial_ends_on=tenant.trial_ends_on)
    key = f"trial_ended:{tenant.pk}:{tenant.trial_ends_on}"
    sent = send(
        "trial_ended",
        to=tenant_emails(tenant),
        audience=AUDIENCE_TENANT,
        language=conf.customer_language(),
        tenant=tenant,
        object_ref=f"tenant:{tenant.pk}",
        dedupe_key=key,
        context=context,
    )
    sent += send(
        "trial_ended",
        to=operator_emails(),
        audience=AUDIENCE_OPERATOR,
        language=conf.operator_language(),
        tenant=tenant,
        object_ref=f"tenant:{tenant.pk}",
        dedupe_key=key,
        context={**context, "for_operator": True, "schema": tenant.schema_name},
    )
    return sent


def tenant_suspended(tenant):
    return send(
        "tenant_suspended",
        to=tenant_emails(tenant),
        audience=AUDIENCE_TENANT,
        language=conf.customer_language(),
        tenant=tenant,
        object_ref=f"tenant:{tenant.pk}",
        context=_tenant_context(tenant),
    )


def tenant_resumed(tenant):
    return send(
        "tenant_resumed",
        to=tenant_emails(tenant),
        audience=AUDIENCE_TENANT,
        language=conf.customer_language(),
        tenant=tenant,
        object_ref=f"tenant:{tenant.pk}",
        context=_tenant_context(tenant),
    )


def _invoice_recipients(doc):
    tenant = doc.tenant
    return _clean([doc.buyer_email or _billing_email(tenant) or tenant.contact_email])


def invoice_issued(doc):
    tenant = doc.tenant
    return send(
        "invoice_issued",
        to=_invoice_recipients(doc),
        audience=AUDIENCE_TENANT,
        language=conf.customer_language(),
        tenant=tenant,
        object_ref=f"billing_document:{doc.pk}",
        dedupe_key=f"invoice_issued:{doc.pk}",
        context=_tenant_context(
            tenant,
            number=doc.number,
            total=f"{doc.total:,.2f}",
            due_date=doc.due_date,
        ),
    )


def payment_confirmed(payment):
    invoice = payment.invoice
    tenant = invoice.tenant
    receipt = getattr(payment, "receipt", None)
    return send(
        "payment_confirmed",
        to=_invoice_recipients(invoice),
        audience=AUDIENCE_TENANT,
        language=conf.customer_language(),
        tenant=tenant,
        object_ref=f"payment:{payment.pk}",
        dedupe_key=f"payment_confirmed:{payment.pk}",
        context=_tenant_context(
            tenant,
            invoice_number=invoice.number,
            amount=f"{payment.gross_amount:,.2f}",
            payment_date=payment.payment_date,
            receipt_number=getattr(receipt, "number", "") or "",
        ),
    )


def billing_event(event, obj):
    """Entry point for orvahr_billing (``orvahr_billing.integration.notify``)."""
    handlers = {
        "invoice_issued": invoice_issued,
        "payment_confirmed": payment_confirmed,
    }
    handler = handlers.get(event)
    if handler is None:
        logger.warning("Unknown billing notification event %r", event)
        return []
    return handler(obj)


def recent(limit=200):
    """Latest log rows for the console."""
    from orvahr_saas.models import NotificationLog

    with use_schema(public_schema_name()):
        return list(NotificationLog.objects.select_related("tenant")[:limit])


__all__ = [
    "EVENTS",
    "billing_event",
    "build_public_message",
    "public_email_connection",
    "render",
    "send",
]
