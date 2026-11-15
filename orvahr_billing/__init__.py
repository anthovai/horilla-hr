"""
OrvaHR SaaS billing -- public schema only, installed only in SaaS mode.

Manual invoicing, no payment gateway: the operator issues invoices
(ใบแจ้งหนี้), records bank transfers (slip, withholding tax and its 50 ทวิ
number), and a tax invoice/receipt (ใบกำกับภาษี/ใบเสร็จรับเงิน) is issued
automatically for each payment; corrections are credit notes (ใบลดหนี้).
Legal background and immutability: ``orvahr_billing.models``.

Operator UI: /billing/ on the public host, for active staff console
operators (``orvahr_saas.ConsoleOperator``; see ``orvahr_billing.auth``).
A tenant-facing, read-only "my invoices" page is out of scope for now.

Daily jobs (public schema):

    python manage.py billing_generate [--date D] [--draft] [--dry-run]
    python manage.py billing_overdue  [--date D] [--grace-days N] [--dry-run] [--suspend]

Settings (all optional; the first two may also be environment variables):

    ORVAHR_BILLING_AUTO_SUSPEND         False. Only when True does billing_overdue
                                        suspend tenants past the grace period.
    ORVAHR_BILLING_SUSPEND_GRACE_DAYS   14 days after the due date.
    ORVAHR_BILLING_PRIVATE_ROOT         <BASE_DIR>/private/orvahr_billing (slips, logo).
    ORVAHR_BILLING_EMAIL_BACKEND        mail backend for sending documents (default:
                                        EMAIL_BACKEND, or SMTP when that is Horilla's
                                        per-tenant backend).
    ORVAHR_BILLING_FROM_EMAIL           sender (default: seller e-mail, DEFAULT_FROM_EMAIL).
    ORVAHR_BILLING_OPERATOR_RESOLVER    dotted path f(request) -> operator | None.
    ORVAHR_BILLING_SUSPEND_FUNC / ORVAHR_BILLING_RESUME_FUNC
                                        override orvahr_saas.services.suspend_tenant / resume_tenant.
"""
