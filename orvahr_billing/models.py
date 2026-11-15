"""
Billing records, all in the public schema (SaaS mode only).

Tax-document rules this module implements
=========================================

Full tax invoice (ใบกำกับภาษีเต็มรูป), Revenue Code section 86/4. The
minimum particulars, as listed in Revenue Department order ป.86/2542 clause 4
(https://www.rd.go.th/3568.html), which restates section 86/4 (1)-(7):

1. the words "ใบกำกับภาษี" in a prominent place;
2. name, address and tax identification number of the VAT registrant who
   issues it;
3. name and address of the purchaser / service recipient;
4. serial number of the tax invoice (and of the book, if any);
5. name, type, category, quantity and value of the goods or services;
6. the VAT amount, shown separately from the value of the goods/services;
7. the date of issue (Buddhist or Christian era allowed).

Section 86/4 (8) "other particulars prescribed by the Director-General" are,
since 1 January 2015, Director-General notification on VAT No. 199 (26 Dec
2013, https://www.rd.go.th/27982.html; clarification
https://www.rd.go.th/49981.html):

* the issuer's establishment: "สำนักงานใหญ่" (or code 00000) or
  "สาขาที่ NNNNN" as on its ภ.พ.20 (clause 8);
* where the buyer is a VAT registrant: the buyer's tax ID (clause 7) and
  establishment (head office / branch number, clause 9). Without them the
  buyer cannot claim the input VAT (section 82/5(2)).

``BillingDocument`` snapshots every one of these at issue time
(``seller_*``, ``buyer_*``, ``number``, lines, ``vat_amount``,
``issue_date``) and the PDF prints them. A combined "ใบกำกับภาษี/ใบเสร็จรับเงิน"
is allowed by clause 6 of ป.86/2542 (a tax invoice combined with another
commercial document such as a receipt).

Credit note (ใบลดหนี้), section 86/10. NOT verified against rd.go.th in
this change -- the list below is from general knowledge of the section and
must be checked by an accountant: the word "ใบลดหนี้" prominently; issuer
name/address/tax ID; buyer name/address; serial number; date; number and
date of the original tax invoice, its value, the correct value, the
difference and the VAT on the difference; a short reason. Notification 199's
establishment / buyer-tax-ID rules are applied to it as well.

Statutory values -- VAT rate (7 % since the 1999 temporary reduction from
10 %, extended year by year by royal decree) and the usual 3 % service WHT
-- are DATA (``SellerSettings.vat_rate``, per-payment WHT), never constants:
verify them against current law.

Immutability
============

Once ``status`` is ``issued`` a document's number, parties, lines and
amounts never change: ``save()`` refuses, and a PostgreSQL trigger (migration
0002) refuses even ``QuerySet.update()`` and raw SQL. Only bookkeeping
fields (``payment_status``, ``settled_amount``, ``credited_amount``,
``overdue_since``, ``emailed_at``) move. Corrections are credit notes.
"""

from datetime import date

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from employee.thai_validators import normalize_digits, validate_thai_national_id
from orvahr_billing.storage import private_storage

HEAD_OFFICE = "00000"

branch_validator = RegexValidator(
    r"^\d{5}$", _("Branch number must be 5 digits (00000 = head office).")
)


def validate_tax_id(value):
    """13-digit Thai tax ID with a valid mod-11 check digit."""
    validate_thai_national_id(value)


class ImmutableDocumentError(Exception):
    """An issued document (or its lines / payment) was about to be changed."""


# ----------------------------------------------------------------- seller


class SellerSettings(models.Model):
    """The SaaS operator's own legal details. A singleton (pk=1)."""

    legal_name_th = models.CharField(_("Legal name (Thai)"), max_length=255)
    legal_name_en = models.CharField(
        _("Legal name (English)"), max_length=255, blank=True
    )
    tax_id = models.CharField(_("Tax ID"), max_length=13, validators=[validate_tax_id])
    branch_code = models.CharField(
        _("Branch number"),
        max_length=5,
        default=HEAD_OFFICE,
        validators=[branch_validator],
        help_text=_(
            "00000 for head office, otherwise the branch number on the VAT registration (ภ.พ.20)."
        ),
    )
    address_th = models.TextField(_("Address (Thai)"))
    address_en = models.TextField(_("Address (English)"), blank=True)
    phone = models.CharField(_("Phone"), max_length=50, blank=True)
    email = models.EmailField(_("E-mail"), blank=True)
    vat_registered = models.BooleanField(_("VAT registered"), default=True)
    vat_rate = models.DecimalField(
        _("VAT rate (%)"),
        max_digits=5,
        decimal_places=2,
        default=7,
        validators=[MinValueValidator(0)],
        help_text=_("Statutory value: verify against current law before issuing."),
    )
    payment_terms_days = models.PositiveIntegerField(
        _("Payment terms (days)"), default=15
    )
    invoice_footer = models.TextField(_("Invoice footer"), blank=True)
    logo = models.ImageField(
        _("Logo"), upload_to="seller/", storage=private_storage, blank=True
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Seller settings")

    def __str__(self):
        return self.legal_name_th

    def save(self, *args, **kwargs):
        self.pk = 1
        self.tax_id = normalize_digits(self.tax_id)
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        """The settings row, or an unsaved blank instance."""
        return cls.objects.filter(pk=1).first() or cls(pk=1)

    def missing_for_issue(self):
        """Labels of the fields a tax document cannot be issued without."""
        missing = []
        if not self.pk or not SellerSettings.objects.filter(pk=1).exists():
            return [_("Seller settings")]
        for field in ("legal_name_th", "tax_id", "address_th"):
            if not getattr(self, field):
                missing.append(self._meta.get_field(field).verbose_name)
        return missing


class BankAccount(models.Model):
    """Where customers transfer money; printed on invoices."""

    bank_name = models.CharField(_("Bank"), max_length=120)
    branch = models.CharField(_("Bank branch"), max_length=120, blank=True)
    account_name = models.CharField(_("Account name"), max_length=255)
    account_number = models.CharField(_("Account number"), max_length=50)
    is_active = models.BooleanField(_("Show on invoices"), default=True)
    sequence = models.PositiveIntegerField(_("Order"), default=0)

    class Meta:
        ordering = ["sequence", "pk"]

    def __str__(self):
        return f"{self.bank_name} {self.account_number}"

    def as_snapshot(self):
        return {
            "bank_name": self.bank_name,
            "branch": self.branch,
            "account_name": self.account_name,
            "account_number": self.account_number,
        }


# ---------------------------------------------------------------- pricing


class BillingCycle(models.TextChoices):
    MONTHLY = "monthly", _("Monthly")
    YEARLY = "yearly", _("Yearly")


class PlanPrice(models.Model):
    """
    Price of a plan for one billing cycle, from ``effective_from`` on.

    ``plan_code`` is the code of ``orvahr_saas.Plan`` when that model exists
    (resolved lazily by ``plan``), or the free-text ``Tenant.plan_code``.
    Amount per period = ``base_price`` + ``per_employee_price`` x active
    employees counted in the tenant's schema on the invoice date. Prices are
    VAT-exclusive.
    """

    plan_code = models.CharField(_("Plan code"), max_length=50, db_index=True)
    cycle = models.CharField(
        _("Billing cycle"), max_length=10, choices=BillingCycle.choices
    )
    base_price = models.DecimalField(
        _("Base price (THB, excl. VAT)"),
        max_digits=14,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
    )
    per_employee_price = models.DecimalField(
        _("Price per active employee (THB, excl. VAT)"),
        max_digits=14,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
    )
    effective_from = models.DateField(_("Effective from"))
    description = models.CharField(
        _("Line description"),
        max_length=255,
        blank=True,
        help_text=_("Printed on the invoice line; defaults to the plan name."),
    )

    class Meta:
        ordering = ["plan_code", "cycle", "-effective_from"]
        constraints = [
            models.UniqueConstraint(
                fields=["plan_code", "cycle", "effective_from"],
                name="orvahr_billing_price_unique_from",
            )
        ]

    def __str__(self):
        return f"{self.plan_code} {self.cycle} {self.effective_from}"

    @property
    def is_per_employee(self):
        return bool(self.per_employee_price)

    @property
    def plan(self):
        """The orvahr_saas.Plan with this code, or None (model may not exist)."""
        from orvahr_billing import integration

        return integration.get_plan(self.plan_code)

    @classmethod
    def current(cls, plan_code, cycle, on_date):
        return (
            cls.objects.filter(
                plan_code=plan_code, cycle=cycle, effective_from__lte=on_date
            )
            .order_by("-effective_from")
            .first()
        )


# --------------------------------------------------------------- customers


class BillingProfile(models.Model):
    """The customer's legal details as they want them on tax invoices."""

    tenant = models.OneToOneField(
        "orvahr_saas.Tenant",
        on_delete=models.PROTECT,
        related_name="billing_profile",
        verbose_name=_("Tenant"),
    )
    legal_name = models.CharField(_("Legal name"), max_length=255)
    tax_id = models.CharField(
        _("Tax ID"), max_length=13, blank=True, validators=[validate_tax_id]
    )
    is_vat_registered = models.BooleanField(_("VAT registered"), default=True)
    branch_code = models.CharField(
        _("Branch number"),
        max_length=5,
        default=HEAD_OFFICE,
        blank=True,
        validators=[branch_validator],
    )
    address = models.TextField(_("Address"))
    email = models.EmailField(_("Invoice e-mail"), blank=True)
    contact_name = models.CharField(_("Contact name"), max_length=255, blank=True)
    phone = models.CharField(_("Phone"), max_length=50, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.legal_name

    def clean(self):
        self.tax_id = normalize_digits(self.tax_id) or ""
        if self.is_vat_registered:
            errors = {}
            if not self.tax_id:
                errors["tax_id"] = _("Required for a VAT-registered customer.")
            if not self.branch_code:
                errors["branch_code"] = _("Required for a VAT-registered customer.")
            if errors:
                raise ValidationError(errors)


class SubscriptionStatus(models.TextChoices):
    ACTIVE = "active", _("Active")
    PAUSED = "paused", _("Paused")
    CANCELLED = "cancelled", _("Cancelled")


class Subscription(models.Model):
    tenant = models.OneToOneField(
        "orvahr_saas.Tenant",
        on_delete=models.PROTECT,
        related_name="billing_subscription",
        verbose_name=_("Tenant"),
    )
    plan_code = models.CharField(_("Plan code"), max_length=50)
    cycle = models.CharField(
        _("Billing cycle"),
        max_length=10,
        choices=BillingCycle.choices,
        default=BillingCycle.MONTHLY,
    )
    start_date = models.DateField(_("Start date"))
    next_invoice_date = models.DateField(
        _("Next invoice date"),
        help_text=_("Start of the next period to invoice (billed in advance)."),
    )
    status = models.CharField(
        _("Status"),
        max_length=10,
        choices=SubscriptionStatus.choices,
        default=SubscriptionStatus.ACTIVE,
    )
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.tenant} {self.plan_code}/{self.cycle}"


# --------------------------------------------------------------- documents


class DocType(models.TextChoices):
    INVOICE = "invoice", _("Invoice")
    RECEIPT = "receipt", _("Tax invoice / receipt")
    CREDIT_NOTE = "credit_note", _("Credit note")


DOC_PREFIX = {
    DocType.INVOICE: "INV",
    DocType.RECEIPT: "RC",
    DocType.CREDIT_NOTE: "CN",
}


class DocumentSequence(models.Model):
    """
    Last number used per document type and Buddhist year. Rows are locked
    with SELECT ... FOR UPDATE by ``orvahr_billing.numbering`` inside the
    transaction that issues the document, so numbers are gap-free.
    """

    doc_type = models.CharField(max_length=20, choices=DocType.choices)
    be_year = models.PositiveIntegerField()
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["doc_type", "be_year"], name="orvahr_billing_seq_unique"
            )
        ]

    def __str__(self):
        return f"{self.doc_type} {self.be_year}: {self.last_number}"


class DocStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    ISSUED = "issued", _("Issued")


class PaymentStatus(models.TextChoices):
    UNPAID = "unpaid", _("Unpaid")
    PARTIAL = "partial", _("Partially paid")
    PAID = "paid", _("Paid")
    CREDITED = "credited", _("Settled by credit note")


# Columns an issued document may still change (everything else is frozen;
# the trigger in migration 0002 enforces the same list).
MUTABLE_AFTER_ISSUE = (
    "payment_status",
    "settled_amount",
    "credited_amount",
    "overdue_since",
    "emailed_at",
    "updated_at",
)


class BillingDocument(models.Model):
    doc_type = models.CharField(_("Type"), max_length=20, choices=DocType.choices)
    number = models.CharField(
        _("Number"), max_length=30, unique=True, null=True, blank=True
    )
    status = models.CharField(
        _("Status"), max_length=10, choices=DocStatus.choices, default=DocStatus.DRAFT
    )
    tenant = models.ForeignKey(
        "orvahr_saas.Tenant",
        on_delete=models.PROTECT,
        related_name="billing_documents",
        verbose_name=_("Tenant"),
    )
    subscription = models.ForeignKey(
        Subscription,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="documents",
    )
    related_document = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="derived_documents",
        verbose_name=_("Related document"),
    )
    issue_date = models.DateField(_("Issue date"), default=date.today)
    due_date = models.DateField(_("Due date"), null=True, blank=True)
    period_start = models.DateField(_("Period start"), null=True, blank=True)
    period_end = models.DateField(_("Period end"), null=True, blank=True)

    # Seller, snapshotted at issue (section 86/4 (2), notification 199 cl. 8).
    seller_name_th = models.CharField(max_length=255, blank=True)
    seller_name_en = models.CharField(max_length=255, blank=True)
    seller_tax_id = models.CharField(max_length=13, blank=True)
    seller_branch_code = models.CharField(max_length=5, blank=True)
    seller_address_th = models.TextField(blank=True)
    seller_address_en = models.TextField(blank=True)
    seller_phone = models.CharField(max_length=50, blank=True)
    seller_email = models.CharField(max_length=254, blank=True)
    seller_vat_registered = models.BooleanField(default=True)
    bank_accounts = models.JSONField(default=list, blank=True)
    footer = models.TextField(blank=True)

    # Buyer, snapshotted at issue (86/4 (3), notification 199 cl. 7, 9).
    buyer_name = models.CharField(_("Customer"), max_length=255, blank=True)
    buyer_tax_id = models.CharField(max_length=13, blank=True)
    buyer_branch_code = models.CharField(max_length=5, blank=True)
    buyer_address = models.TextField(blank=True)
    buyer_vat_registered = models.BooleanField(default=False)
    buyer_email = models.CharField(max_length=254, blank=True)

    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    subtotal = models.DecimalField(
        _("Subtotal"), max_digits=14, decimal_places=2, default=0
    )
    vat_amount = models.DecimalField(
        _("VAT"), max_digits=14, decimal_places=2, default=0
    )
    total = models.DecimalField(_("Total"), max_digits=14, decimal_places=2, default=0)
    amount_in_words = models.CharField(max_length=500, blank=True)
    notes = models.TextField(_("Notes"), blank=True)

    # Receipt: withholding tax deducted by the customer on this payment.
    wht_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    wht_certificate_no = models.CharField(max_length=100, blank=True)
    net_received = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    # Credit note (section 86/10).
    cn_reason = models.TextField(_("Reason"), blank=True)
    cn_original_value = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    cn_correct_value = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )

    # Bookkeeping (mutable after issue).
    payment_status = models.CharField(
        _("Payment"),
        max_length=10,
        choices=PaymentStatus.choices,
        default=PaymentStatus.UNPAID,
    )
    settled_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    credited_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    overdue_since = models.DateField(_("Overdue since"), null=True, blank=True)
    emailed_at = models.DateTimeField(null=True, blank=True)

    issued_at = models.DateTimeField(null=True, blank=True)
    issued_by = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.CharField(max_length=150, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-issue_date", "-pk"]
        constraints = [
            # One invoice per subscription period: generation is idempotent.
            models.UniqueConstraint(
                fields=["subscription", "period_start"],
                condition=models.Q(doc_type="invoice"),
                name="orvahr_billing_one_invoice_per_period",
            ),
            models.CheckConstraint(
                condition=models.Q(status="draft") | models.Q(number__isnull=False),
                name="orvahr_billing_issued_has_number",
            ),
        ]
        indexes = [models.Index(fields=["doc_type", "status", "payment_status"])]

    def __str__(self):
        return self.number or f"{self.get_doc_type_display()} (draft #{self.pk})"

    # -- immutability --------------------------------------------------

    @property
    def is_issued(self):
        return self.status == DocStatus.ISSUED

    def save(self, *args, **kwargs):
        if self.pk:
            stored = BillingDocument.objects.filter(pk=self.pk).values().first()
            if stored and stored["status"] == DocStatus.ISSUED:
                changed = [
                    f.attname
                    for f in self._meta.concrete_fields
                    if f.attname not in MUTABLE_AFTER_ISSUE
                    and stored.get(f.attname) != getattr(self, f.attname)
                ]
                if changed:
                    raise ImmutableDocumentError(
                        f"{stored['number']} is issued; cannot change {', '.join(changed)}. "
                        "Issue a credit note instead."
                    )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.is_issued:
            raise ImmutableDocumentError(
                f"{self.number} is issued; it cannot be deleted."
            )
        return super().delete(*args, **kwargs)

    # -- presentation --------------------------------------------------

    @property
    def is_tax_invoice(self):
        """A receipt is also a tax invoice only when the seller is VAT registered."""
        return self.doc_type == DocType.RECEIPT and self.seller_vat_registered

    @property
    def title_th(self):
        if self.doc_type == DocType.INVOICE:
            return "ใบแจ้งหนี้"
        if self.doc_type == DocType.CREDIT_NOTE:
            return "ใบลดหนี้"
        return "ใบกำกับภาษี/ใบเสร็จรับเงิน" if self.is_tax_invoice else "ใบเสร็จรับเงิน"

    @property
    def title_en(self):
        if self.doc_type == DocType.INVOICE:
            return "Invoice"
        if self.doc_type == DocType.CREDIT_NOTE:
            return "Credit note"
        return "Tax invoice / Receipt" if self.is_tax_invoice else "Receipt"

    @property
    def outstanding(self):
        """Invoices only: total less payments (incl. WHT) and credit notes."""
        if self.doc_type != DocType.INVOICE:
            return None
        return self.total - self.settled_amount - self.credited_amount

    @property
    def cn_difference(self):
        return self.subtotal


class BillingLine(models.Model):
    document = models.ForeignKey(
        BillingDocument, on_delete=models.CASCADE, related_name="lines"
    )
    position = models.PositiveIntegerField(default=0)
    description = models.CharField(_("Description"), max_length=500)
    quantity = models.DecimalField(
        _("Quantity"), max_digits=12, decimal_places=2, default=1
    )
    unit = models.CharField(_("Unit"), max_length=30, blank=True)
    unit_price = models.DecimalField(
        _("Unit price"), max_digits=14, decimal_places=2, default=0
    )
    amount = models.DecimalField(
        _("Amount"), max_digits=14, decimal_places=2, default=0
    )

    class Meta:
        ordering = ["position", "pk"]

    def __str__(self):
        return self.description

    def _parent_issued(self):
        return BillingDocument.objects.filter(
            pk=self.document_id, status=DocStatus.ISSUED
        ).exists()

    def save(self, *args, **kwargs):
        if self._parent_issued():
            raise ImmutableDocumentError("Lines of an issued document cannot change.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self._parent_issued():
            raise ImmutableDocumentError("Lines of an issued document cannot change.")
        return super().delete(*args, **kwargs)


def slip_upload_to(instance, filename):
    import os
    import uuid

    ext = os.path.splitext(filename)[1].lower()[:10]
    return f"slips/{timezone.now():%Y/%m}/{uuid.uuid4().hex}{ext}"


class Payment(models.Model):
    """A bank transfer received against an invoice. Recorded once, never edited."""

    invoice = models.ForeignKey(
        BillingDocument,
        on_delete=models.PROTECT,
        related_name="payments",
        limit_choices_to={"doc_type": DocType.INVOICE},
    )
    payment_date = models.DateField(_("Payment date"))
    amount_received = models.DecimalField(
        _("Amount received (THB)"),
        max_digits=14,
        decimal_places=2,
        validators=[MinValueValidator(0)],
    )
    wht_amount = models.DecimalField(
        _("Withholding tax deducted (THB)"),
        max_digits=14,
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)],
    )
    wht_certificate_no = models.CharField(
        _("WHT certificate (50 ทวิ) no."), max_length=100, blank=True
    )
    reference = models.CharField(_("Transfer reference"), max_length=255, blank=True)
    bank_account = models.ForeignKey(
        BankAccount,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        verbose_name=_("Received into"),
    )
    slip = models.FileField(
        _("Transfer slip"),
        upload_to=slip_upload_to,
        storage=private_storage,
        blank=True,
    )
    note = models.TextField(_("Note"), blank=True)
    receipt = models.OneToOneField(
        BillingDocument,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="source_payment",
    )
    recorded_by = models.CharField(max_length=150, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-payment_date", "-pk"]

    def __str__(self):
        return f"{self.invoice} {self.payment_date} {self.gross_amount}"

    @property
    def gross_amount(self):
        """What the payment settles: cash received + WHT the customer kept."""
        return (self.amount_received or 0) + (self.wht_amount or 0)

    def save(self, *args, **kwargs):
        if self.pk:
            stored = Payment.objects.filter(pk=self.pk).values().first()
            if stored:
                changed = [
                    f.attname
                    for f in self._meta.concrete_fields
                    if f.attname != "receipt_id"
                    and stored.get(f.attname) != getattr(self, f.attname)
                ]
                if changed or (
                    stored["receipt_id"] and stored["receipt_id"] != self.receipt_id
                ):
                    raise ImmutableDocumentError(
                        "A recorded payment cannot be changed."
                    )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableDocumentError("A recorded payment cannot be deleted.")
