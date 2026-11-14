"""
Create common Thai-law-based leave types as STARTING POINTS.

Every figure below is an unverified starting point drawn from the Labour
Protection Act B.E. 2541 (and related social-security rules) as commonly
summarised. Verify against the current law, the employment contract and your
company policy before use; amend the created types in the UI. LeaveType has
no description field, so this note is printed by the command instead.

Idempotent: a type is matched on name + company and never modified once it
exists.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from base.models import Company
from leave.models import LeaveType

YEARLY = {"reset": True, "reset_based": "yearly", "reset_month": "1", "reset_day": "1"}

# name must be <= 30 chars (LeaveType.name max_length)
THAI_LEAVE_TYPES = [
    {
        "name": "Sick Leave",
        "note": "LPA: paid up to 30 days/yr; employer may ask for a medical certificate for 3+ days.",
        "fields": dict(payment="paid", payment_type="paid", count=30, total_days=30,
                       require_attachment="no", exclude_holiday="yes",
                       exclude_company_leave="yes", **YEARLY),
    },
    {
        "name": "Personal Business Leave",
        "note": "ลากิจ: law gives the right but no statutory day count; 3 paid days is a COMPANY-POLICY placeholder.",
        "fields": dict(payment="paid", payment_type="paid", count=3, total_days=3,
                       exclude_holiday="yes", exclude_company_leave="yes", **YEARLY),
    },
    {
        "name": "Annual Vacation",
        "note": "LPA: at least 6 working days/yr after 1 year of service. The 1-year eligibility is NOT enforced here; add a service-duration condition if wanted.",
        "fields": dict(payment="paid", payment_type="paid", count=6, total_days=6,
                       exclude_holiday="yes", exclude_company_leave="yes", **YEARLY),
    },
    {
        "name": "Maternity Leave",
        "note": "LPA/SSO: 98 days including holidays; employer pays wages for the first 45 days. Modelled as custom payment = 45/98 = 45.92% of the period (an approximation of '45 paid days'); social-security benefit for the remainder is NOT modelled.",
        "fields": dict(payment="paid", payment_type="custom",
                       payment_percentage=Decimal("45.92"), count=98, total_days=98,
                       require_attachment="yes", exclude_holiday="no",
                       exclude_company_leave="no", reset=False),
    },
    {
        "name": "Ordination Leave",
        "note": "อุปสมบท: 120 days once per employment is a COMPANY-POLICY placeholder, unpaid by default; no private-sector statutory entitlement assumed.",
        "fields": dict(payment="unpaid", payment_type="unpaid", count=120, total_days=120,
                       exclude_holiday="no", exclude_company_leave="no", reset=False),
    },
    {
        "name": "Military Service Leave",
        "note": "LPA: leave for military duty; wages commonly summarised as payable up to 60 days/yr. Verify.",
        "fields": dict(payment="paid", payment_type="paid", count=60, total_days=60,
                       require_attachment="yes", **YEARLY),
    },
    {
        "name": "Sterilisation Leave",
        "note": "LPA: leave for sterilisation per medical certificate, paid; day count follows the certificate, so no limit is set.",
        "fields": dict(payment="paid", payment_type="paid", limit_leave=False,
                       count=None, total_days=None, require_attachment="yes", reset=False),
    },
    {
        "name": "Training Leave",
        "note": "LPA: leave for training/skill development with advance notice; 30 days/yr unpaid is a placeholder (law caps and conditions vary; pay is policy).",
        "fields": dict(payment="unpaid", payment_type="unpaid", count=30, total_days=30,
                       exclude_holiday="yes", exclude_company_leave="yes", **YEARLY),
    },
]


class Command(BaseCommand):
    help = "Create common Thai leave types as starting points (idempotent). Verify all figures."

    def add_arguments(self, parser):
        parser.add_argument("--company", type=int, help="Company ID (default: global)")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        company = None
        if opts["company"] is not None:
            try:
                company = Company.objects.get(pk=opts["company"])
            except Company.DoesNotExist:
                raise CommandError(f"Company {opts['company']} not found")
        dry = opts["dry_run"]
        created = existing = 0
        with transaction.atomic():
            for spec in THAI_LEAVE_TYPES:
                if LeaveType.objects.filter(name=spec["name"], company_id=company).exists():
                    existing += 1
                    self.stdout.write(f"exists  {spec['name']}")
                    continue
                created += 1
                self.stdout.write(
                    f"{'would create' if dry else 'create'} {spec['name']} - {spec['note']}"
                )
                if not dry:
                    LeaveType.objects.create(
                        name=spec["name"], company_id=company, **spec["fields"]
                    )
        self.stderr.write(
            self.style.WARNING(
                "NOTE: all figures are unverified starting points; check current "
                "Thai law, contracts and company policy, then edit in the UI."
            )
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"{'[dry-run] ' if dry else ''}{created} to create, {existing} existing."
            )
        )
