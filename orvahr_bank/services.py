"""
Build a PayBatch for services/exports from a payroll run and fetch the file.

Rules that matter:

* Every payslip of the run is accounted for. A person who cannot be paid by
  file (no account, malformed account, no bank, no valid national ID, a
  negative net) is listed as a problem with the reason; nobody is dropped
  silently. Zero-net payslips are listed as "nothing to transfer".
* Money is converted to integer satang once, here, with Decimal.
* Only what a transfer file needs leaves this process (data minimisation):
  name, employee code, national ID (the exporter's validator requires it),
  bank, account and net. Tax and SSO splits are not sent; a line is sent as
  gross == net with no deductions, which is what the transfer amounts to.
* Nothing here logs an account number, ID or amount.
"""

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from email.message import Message

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import DatabaseError, transaction
from django.utils.translation import gettext as _

from employee.models import EmployeeThaiProfile
from employee.thai_validators import (
    is_valid_thai_national_id,
    mask_id,
    normalize_digits,
)
from orvahr_bank.models import account_number_validator, satang_to_baht

logger = logging.getLogger(__name__)

DEFAULT_EXPORTS_URL = "http://exports:8091"
TIMEOUT_SECONDS = 20
ISSUED_PAYSLIP_STATUSES = ("confirmed", "paid")
BANK_NAMES = dict(EmployeeThaiProfile.BANK_CHOICES)


def to_satang(value):
    """Float/Decimal baht -> int satang, half-up on the 3rd decimal."""
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return int(amount * 100)


@dataclass
class PreviewRow:
    employee: object
    bank_name: str = ""
    masked_account: str = ""
    net_satang: int = 0
    problem: str = ""
    note: str = ""

    @property
    def net_baht(self):
        return satang_to_baht(self.net_satang)


@dataclass
class BuiltBatch:
    payload: dict
    rows: list = field(default_factory=list)
    # Problems with the run/company as a whole (not one person).
    batch_problems: list = field(default_factory=list)
    # Index into payload["lines"] -> row, to map exporter errors back.
    line_rows: list = field(default_factory=list)

    @property
    def problem_rows(self):
        return [r for r in self.rows if r.problem]

    @property
    def line_count(self):
        return len(self.payload["lines"])

    @property
    def total_satang(self):
        return sum(line["net"] for line in self.payload["lines"])

    @property
    def total_baht(self):
        return satang_to_baht(self.total_satang)

    def fingerprint(self):
        """Changes when what would be exported changes (count/total/people)."""
        ids = ",".join(str(r.employee.pk) for r in self.line_rows)
        return f"{self.line_count}:{self.total_satang}:{ids}"


def _related(obj, name):
    try:
        return getattr(obj, name)
    except ObjectDoesNotExist:
        return None


def employer_identity(company):
    """
    (legal name, tax id) from orvahr_filing's CompanyTaxProfile when present.
    Looked up loosely so this app does not depend on that app's migrations.
    """
    try:
        # Savepoint: a missing table must not poison the request's transaction.
        with transaction.atomic():
            profile = _related(company, "thai_tax_profile")
    except DatabaseError:
        profile = None
    if profile is None:
        return company.company, ""
    return (profile.legal_name_th or company.company), (profile.tax_id or "")


def build_pay_batch(payroll_batch, account, effective_date):
    """Return a BuiltBatch for ``payroll_batch`` paid from ``account``."""
    from payroll.models.models import Payslip

    company = account.company_id
    name, tax_id = employer_identity(company)
    built = BuiltBatch(
        payload={
            "employer": {
                "name": name,
                "tax_id": tax_id,
                "bank_code": account.bank_code,
                "account_no": account.account_number,
            },
            "period": payroll_batch.period_end.strftime("%Y-%m"),
            "pay_date": effective_date.isoformat(),
            "lines": [],
        }
    )
    if payroll_batch.company_id_id and payroll_batch.company_id_id != company.pk:
        built.batch_problems.append(
            _("The payroll run and the bank account belong to different companies.")
        )
    if not is_valid_thai_national_id(tax_id or ""):
        built.batch_problems.append(
            _(
                "The company has no valid 13-digit tax ID. Set it in the company's "
                "Thai tax profile."
            )
        )

    payslips = (
        Payslip.objects.entire()
        .filter(
            payroll_batch=payroll_batch,
            status__in=ISSUED_PAYSLIP_STATUSES,
        )
        .select_related(
            "employee_id",
            "employee_id__employee_bank_details",
            "employee_id__thai_profile",
        )
        .order_by("employee_id__employee_first_name", "employee_id__pk", "pk")
    )
    seen = set()
    for slip in payslips:
        employee = slip.employee_id
        row = PreviewRow(employee=employee, net_satang=to_satang(slip.net_pay))
        built.rows.append(row)

        profile = _related(employee, "thai_profile")
        bank = _related(employee, "employee_bank_details")
        account_no = normalize_digits(getattr(bank, "account_number", None)) or ""
        bank_code = getattr(profile, "bank_code", "") or ""
        national_id = getattr(profile, "national_id", None) or ""
        promptpay = getattr(profile, "promptpay_id", None)
        row.bank_name = BANK_NAMES.get(bank_code, "")
        row.masked_account = mask_id(account_no)

        if employee.pk in seen:
            row.problem = _("More than one issued payslip in this run.")
            continue
        seen.add(employee.pk)
        if row.net_satang < 0:
            row.problem = _("Net pay is negative.")
            continue
        if row.net_satang == 0:
            row.note = _("Nothing to transfer (net pay is zero).")
            continue
        if not account_no:
            row.problem = (
                _("No bank account number. PromptPay is not supported by this file.")
                if promptpay
                else _("No bank account number.")
            )
            continue
        try:
            account_number_validator(account_no)
        except Exception:
            row.problem = _("The bank account number is not 10 to 15 digits.")
            continue
        if not bank_code:
            row.problem = _("No bank selected in the Thai profile.")
            continue
        if not is_valid_thai_national_id(national_id):
            row.problem = _("No valid national ID in the Thai profile.")
            continue

        net = row.net_satang
        built.payload["lines"].append(
            {
                "employee_code": employee.badge_id or str(employee.pk),
                "full_name": employee.get_full_name(),
                "national_id": national_id,
                "bank_code": bank_code,
                "account_no": account_no,
                "gross": net,
                "income_tax_withheld": 0,
                "sso_employee": 0,
                "sso_employer": 0,
                "other_deductions": 0,
                "net": net,
            }
        )
        built.line_rows.append(row)

    if not built.rows:
        built.batch_problems.append(
            _("This run has no confirmed or paid payslips.")
        )
    return built


# ------------------------------------------------------------------ HTTP


@dataclass
class ExportResult:
    ok: bool
    data: bytes = b""
    filename: str = ""
    content_type: str = "application/octet-stream"
    error: str = ""
    problems: list = field(default_factory=list)


def exports_url():
    return getattr(settings, "ORVAHR_EXPORTS_URL", DEFAULT_EXPORTS_URL).rstrip("/")


def _filename(header, fallback):
    if not header:
        return fallback
    msg = Message()
    msg["content-disposition"] = header
    name = msg.get_filename() or fallback
    # Never let a header pick a path.
    return name.replace("/", "_").replace("\\", "_")[:150]


def _map_problems(problems, built):
    """'lines[3].x' -> '<employee name>: lines[3].x' so a user can act on it."""
    out = []
    for text in problems or []:
        text = str(text)
        label = ""
        if text.startswith("lines["):
            try:
                index = int(text[6 : text.index("]")])
                label = built.line_rows[index].employee.get_full_name()
            except (ValueError, IndexError):
                label = ""
        out.append(f"{label}: {text}" if label else text)
    return out


def request_export(exporter_id, built):
    """POST the batch to the exports service. Never raises."""
    url = f"{exports_url()}/v1/export/{exporter_id}"
    body = json.dumps(built.payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            data = resp.read()
            return ExportResult(
                ok=True,
                data=data,
                filename=_filename(
                    resp.headers.get("Content-Disposition"), f"{exporter_id}.txt"
                ),
                content_type=resp.headers.get(
                    "Content-Type", "application/octet-stream"
                ),
            )
    except urllib.error.HTTPError as exc:
        logger.warning("exports service answered HTTP %s", exc.code)
        problems = []
        try:
            problems = json.loads(exc.read() or b"{}").get("problems") or []
        except (ValueError, AttributeError, OSError):
            problems = []
        if exc.code == 422:
            return ExportResult(
                ok=False,
                error=_("The export service rejected the data."),
                problems=_map_problems(problems, built),
            )
        if exc.code == 404:
            return ExportResult(
                ok=False, error=_("The export service does not know this file format.")
            )
        return ExportResult(
            ok=False,
            error=_("The export service failed (HTTP %(code)s).") % {"code": exc.code},
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning("exports service unreachable: %s", type(exc).__name__)
        return ExportResult(
            ok=False,
            error=_("The export service could not be reached. Try again later."),
        )
    except Exception:  # never raise into the request
        logger.exception("unexpected error calling the exports service")
        return ExportResult(ok=False, error=_("The export failed unexpectedly."))
