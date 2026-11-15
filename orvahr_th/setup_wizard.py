"""
Thai company setup wizard (ตั้งค่าบริษัทไทย): the services behind each step.

A guided pass over what a fresh company needs before its first Thai payroll.
Every step reuses what the app already has -- the filing app's company tax
profile, the payroll component library and tax packs, the bank, benefits and
PDPA models, the Thai holiday and leave-type commands, master_import -- and
writes nothing of its own except ``ThaiSetupProgress`` (where the company is
in the wizard, and which tax pack new contracts get).

Every step is idempotent: running it again changes only what was asked for,
never creates a second copy of anything, and never overwrites a record that
already exists unless the form edits that very record (the tax profile, the
bank account, the provident fund plan, the DPO contact).

Each step counts as done when its effect is visible in the data (a tax
profile exists, a bank account exists...) or when someone confirmed it in
the wizard. A skipped step stays skipped until it is done.

Nothing here depends on SaaS mode: the wizard works on whichever company it
is given, in a single-tenant install or inside a tenant schema alike.
"""

import io
import os
import tempfile
from dataclasses import dataclass, field

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

TAX_PACK_KEY = "th_2026"
SSO_KEY = "th_sso"
# The overtime / holiday-work entries of the component library offered here.
OT_KEYS = (
    "th_ot_workday",
    "th_holiday_monthly",
    "th_holiday_daily",
    "th_weekly_holiday_monthly",
    "th_weekly_holiday_daily",
)
# Preticked: what a company with monthly-paid staff usually needs.
OT_DEFAULT_KEYS = ("th_ot_workday", "th_holiday_monthly", "th_weekly_holiday_monthly")

DONE = "done"
SKIPPED = "skipped"
TODO = "todo"


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    title_th: str
    optional: bool = False


STEPS = (
    Step("company", _("Company legal & tax"), "ข้อมูลนิติบุคคลและภาษี"),
    Step("payroll", _("Payroll basics"), "พื้นฐานการจ่ายเงินเดือน"),
    Step("bank", _("Payroll bank account"), "บัญชีธนาคารจ่ายเงินเดือน"),
    Step("calendar", _("Holidays & leave"), "วันหยุดและประเภทการลา"),
    Step("benefits", _("Provident fund"), "กองทุนสำรองเลี้ยงชีพ", optional=True),
    Step("pdpa", _("PDPA"), "คุ้มครองข้อมูลส่วนบุคคล"),
    Step("people", _("People"), "พนักงาน"),
    Step("finish", _("Finish"), "สรุป"),
)
STEP_KEYS = tuple(step.key for step in STEPS)
STEP_BY_KEY = {step.key: step for step in STEPS}


def next_key(key):
    index = STEP_KEYS.index(key)
    return STEP_KEYS[min(index + 1, len(STEP_KEYS) - 1)]


def previous_key(key):
    index = STEP_KEYS.index(key)
    return STEP_KEYS[max(index - 1, 0)]


# ------------------------------------------------------------------ access


def can_run_setup(user):
    """Superusers and company administrators (may change the company)."""
    return bool(
        user.is_authenticated
        and (user.is_superuser or user.has_perm("base.change_company"))
    )


def setup_companies(user):
    """The companies ``user`` may run the wizard for."""
    from base.models import Company

    companies = Company.objects.all().order_by("pk")
    if not can_run_setup(user):
        return companies.none()
    if user.is_superuser:
        return companies
    from base.auth_backends import get_allowed_company_ids

    allowed = get_allowed_company_ids(user)
    return companies.filter(pk__in=allowed) if allowed else companies


def default_company(request, companies):
    """The selected company when it is one of ``companies``, else the user's, else the first."""
    from horilla.horilla_middlewares import get_selected_company

    candidates = []
    try:
        selected = get_selected_company()
    except Exception:
        selected = None
    if selected and selected != "all":
        candidates.append(selected)
    try:
        candidates.append(request.user.employee_get.employee_work_info.company_id_id)
    except Exception:
        pass
    for pk in candidates:
        try:
            company = companies.filter(pk=int(pk)).first()
        except (TypeError, ValueError):
            company = None
        if company:
            return company
    return companies.first()


# ---------------------------------------------------------------- progress


def progress_for(company):
    from orvahr_th.models import ThaiSetupProgress

    progress, _created = ThaiSetupProgress.all_objects.get_or_create(company=company)
    return progress


def record(progress, key, state, user=None, current=None):
    """Remember a step as done / skipped and where the wizard is."""
    steps = dict(progress.steps or {})
    if state is None:
        steps.pop(key, None)
    elif not (state == SKIPPED and steps.get(key) == DONE):
        steps[key] = state
    progress.steps = steps
    if current:
        progress.current_step = current
    if user is not None and user.is_authenticated:
        progress.updated_by = user
    progress.save()


def set_current(progress, key):
    if progress.current_step != key:
        progress.current_step = key
        progress.save(update_fields=["current_step", "updated_at"])


def statuses(company, progress, user=None):
    """{step key: done / skipped / todo}: what the data shows, else what was recorded."""
    detected = {
        "company": _has_tax_profile(company),
        "payroll": _payroll_ready(company),
        "bank": _has_bank_account(company),
        "calendar": _calendar_ready(company),
        "benefits": _has_pvd_plan(company),
        "pdpa": _pdpa_ready(company),
        "people": employee_count(company, exclude_user=user) > 0,
        "finish": progress.completed_at is not None,
    }
    recorded = progress.steps or {}
    result = {}
    for key in STEP_KEYS:
        if detected.get(key) or recorded.get(key) == DONE:
            result[key] = DONE
        elif recorded.get(key) == SKIPPED:
            result[key] = SKIPPED
        else:
            result[key] = TODO
    return result


def resume_key(progress, status):
    """Where to pick up: the step last open, else the first one not done."""
    if progress.current_step in STEP_KEYS:
        return progress.current_step
    for key in STEP_KEYS:
        if status[key] == TODO:
            return key
    return "finish"


def is_complete(company):
    """For the dashboard checklist: has the wizard been finished for ``company``?"""
    from orvahr_th.models import ThaiSetupProgress

    if company is None:
        return False
    return ThaiSetupProgress.all_objects.filter(
        company_id=getattr(company, "pk", company), completed_at__isnull=False
    ).exists()


# ---------------------------------------------------- step 1: company / tax


def _has_tax_profile(company):
    from orvahr_filing.models import profile_for

    return profile_for(company) is not None


def save_tax_profile(company, form):
    profile = form.save(commit=False)
    profile.company = company
    profile.save()
    return profile


# -------------------------------------------------------- step 2: payroll


def sso_deduction():
    from payroll.component_library import COMPONENT_LIBRARY
    from payroll.models.models import Deduction

    title = next(e["name"] for e in COMPONENT_LIBRARY if e["key"] == SSO_KEY)
    return Deduction.objects.entire().filter(title__iexact=title).order_by("pk").first()


def thai_tax_pack():
    from payroll.models.models import FilingStatus
    from payroll.tax_packs import TAX_PACKS

    name = next(p["name"] for p in TAX_PACKS if p["key"] == TAX_PACK_KEY)
    return FilingStatus.objects.entire().filter(filing_status=name).order_by("pk").first()


def ot_library_rows():
    """The overtime entries of the component library, flagged when already loaded."""
    from payroll.component_library import library_rows
    from payroll.models.models import Allowance

    titles = Allowance.objects.entire().values_list("title", flat=True)
    return [row for row in library_rows(titles) if row["key"] in OT_KEYS]


def payroll_settings(company):
    from payroll.models.tax_models import PayrollSettings

    return PayrollSettings.objects.entire().filter(company_id=company).first()


def pay_period(company):
    from payroll.models.payroll_batch import PayPeriodSettings

    return PayPeriodSettings.objects.entire().filter(company_id=company).first()


def _payroll_ready(company):
    row = payroll_settings(company)
    return bool(row and row.currency_symbol == "฿" and sso_deduction() is not None)


def apply_payroll(company, progress, data):
    """
    ``data``: use_baht, pay_day (int or None), thai_tax_pack, apply_to_active,
    load_sso, ot_keys. Returns a list of what changed (or was already there).
    """
    from payroll.component_library import load_components
    from payroll.models.models import Contract
    from payroll.models.payroll_batch import PayPeriodSettings
    from payroll.models.tax_models import PayrollSettings
    from payroll.tax_packs import load_tax_packs

    done = []
    with transaction.atomic():
        if data.get("use_baht"):
            row = payroll_settings(company)
            if row is None:
                PayrollSettings(company_id=company, currency_symbol="฿", position="prefix").save()
                done.append(_("Payroll currency set to Thai baht (฿)."))
            elif row.currency_symbol != "฿" or row.position != "prefix":
                row.currency_symbol, row.position = "฿", "prefix"
                row.save()
                done.append(_("Payroll currency set to Thai baht (฿)."))
            else:
                done.append(_("Payroll currency was already Thai baht (฿)."))

        pay_day = data.get("pay_day")
        if pay_day:
            period = pay_period(company) or PayPeriodSettings(company_id=company)
            if period.pk is None or period.pay_day_of_month != pay_day:
                period.pay_day_of_month = pay_day
                period.save()
            done.append(_("Pay day saved."))

        if data.get("thai_tax_pack"):
            load_tax_packs([TAX_PACK_KEY])
            status = thai_tax_pack()
            if status is not None:
                progress.default_filing_status = status
                progress.save(update_fields=["default_filing_status", "updated_at"])
                done.append(_("New contracts will get the Thai income tax pack."))
                if data.get("apply_to_active"):
                    changed = Contract._base_manager.filter(
                        contract_status="active",
                        employee_id__employee_work_info__company_id=company,
                        filing_status__isnull=True,
                    ).update(filing_status=status)
                    if changed:
                        done.append(
                            _("%(count)s active contract(s) without a tax setting were given it.")
                            % {"count": changed}
                        )
        elif progress.default_filing_status_id:
            progress.default_filing_status = None
            progress.save(update_fields=["default_filing_status", "updated_at"])
            done.append(_("New contracts will no longer get a tax pack automatically."))

        if data.get("load_sso"):
            created = load_components([SSO_KEY])
            done.append(
                _("Social security deduction created.")
                if created
                else _("Social security deduction was already there.")
            )

        keys = [k for k in data.get("ot_keys") or () if k in OT_KEYS]
        if keys:
            created = load_components(keys)
            done.append(
                _("%(created)s overtime component(s) created, %(existing)s already there.")
                % {"created": len(created), "existing": len(keys) - len(created)}
            )
    return done


def default_contract_tax_pack(sender, instance, **kwargs):
    """
    pre_save on payroll.Contract: a NEW contract saved without a filing status
    gets the tax pack the company chose in the wizard. An existing contract,
    or one with a filing status already, is never touched.
    """
    if not instance._state.adding or instance.filing_status_id is not None:
        return
    from orvahr_th.models import ThaiSetupProgress

    try:
        company_id = instance.employee_id.employee_work_info.company_id_id
    except Exception:
        return
    if not company_id:
        return
    status_id = (
        ThaiSetupProgress.all_objects.filter(company_id=company_id)
        .values_list("default_filing_status_id", flat=True)
        .first()
    )
    if status_id:
        instance.filing_status_id = status_id


# ----------------------------------------------------------- step 3: bank


def bank_accounts(company):
    from orvahr_bank.models import CompanyBankAccount

    return CompanyBankAccount.objects.entire().filter(company_id=company).order_by("pk")


def _has_bank_account(company):
    return bank_accounts(company).exists()


def save_bank_account(company, form):
    account = form.save(commit=False)
    account.company_id = company
    account.save()
    return account


# ------------------------------------------------------- step 4: calendar


def fixed_holiday_names():
    from base.management.commands.load_thai_holidays import load_fixed_data

    return [item["name"] for item in load_fixed_data()["holidays"]]


def lunar_holiday_labels():
    from base.management.commands.load_thai_holidays import load_fixed_data

    return [item["label"] for item in load_fixed_data()["variable_holidays_expected"]]


def holidays_in_year(company, year):
    from base.models import Holidays

    return Holidays._base_manager.filter(
        Q(company_id__isnull=True) | Q(company_id=company), start_date__year=year
    ).order_by("start_date")


def _has_fixed_holidays(company, year):
    return holidays_in_year(company, year).filter(name__in=fixed_holiday_names()).exists()


def leave_type_rows(company):
    """The Thai leave types the command offers, flagged when already present."""
    from leave.management.commands.load_thai_leave_types import THAI_LEAVE_TYPES
    from leave.models import LeaveType

    present = set(
        LeaveType._base_manager.filter(Q(company_id__isnull=True) | Q(company_id=company))
        .values_list("name", flat=True)
    )
    return [
        {
            "name": spec["name"],
            "note": spec["note"],
            "days": spec["fields"].get("count"),
            "paid": spec["fields"].get("payment") == "paid",
            "exists": spec["name"] in present,
        }
        for spec in THAI_LEAVE_TYPES
    ]


def _calendar_ready(company):
    year = timezone.localdate().year
    return _has_fixed_holidays(company, year) and any(
        row["exists"] for row in leave_type_rows(company)
    )


@dataclass
class HolidayResult:
    created: int = 0
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    missing_lunar: bool = False


def load_holidays(company, year, csv_file=None):
    """
    Run load_thai_holidays for ``year`` (shared by all companies, as the
    tenant provisioning and the demo load them), with the uploaded CSV of
    lunar / substitution days if any. A bad CSV writes nothing.
    """
    before = holidays_in_year(company, year).count()
    out, err = io.StringIO(), io.StringIO()
    path = None
    try:
        options = {"year": year, "stdout": out, "stderr": err}
        if csv_file is not None:
            handle, path = tempfile.mkstemp(suffix=".csv")
            with os.fdopen(handle, "wb") as fh:
                for chunk in csv_file.chunks():
                    fh.write(chunk)
            options["file"] = path
        call_command("load_thai_holidays", **options)
    except CommandError as exc:
        lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
        return HolidayResult(errors=lines or [str(exc)])
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
    warnings = []
    for line in err.getvalue().splitlines():
        line = line.strip()
        if line.startswith("conflict"):
            warnings.append(line)
    return HolidayResult(
        created=holidays_in_year(company, year).count() - before,
        warnings=warnings,
        missing_lunar="variable (lunar) holidays NOT loaded" in err.getvalue(),
    )


def lunar_csv_template():
    """
    A CSV to fill in: the lunar holidays to look up, with the dates left empty
    on purpose (they change every year and are never guessed). Uploaded as it
    is, every row is rejected and nothing is written.
    """
    rows = ["date,name,department"]
    rows += [f",{label} Day," for label in lunar_holiday_labels()]
    rows.append(",Substitution Day,")
    return "\n".join(rows) + "\n"


def create_leave_types(company, names):
    """Create the chosen Thai leave types that do not exist yet (shared, like the command)."""
    from leave.management.commands.load_thai_leave_types import THAI_LEAVE_TYPES
    from leave.models import LeaveType

    wanted = set(names)
    present = {row["name"] for row in leave_type_rows(company) if row["exists"]}
    created = []
    with transaction.atomic():
        for spec in THAI_LEAVE_TYPES:
            if spec["name"] not in wanted or spec["name"] in present:
                continue
            LeaveType.objects.create(name=spec["name"], company_id=None, **spec["fields"])
            created.append(spec["name"])
    return created


# ------------------------------------------------------- step 5: benefits


def pvd_plans(company):
    from orvahr_benefits.models import ProvidentFundPlan

    return ProvidentFundPlan.objects.entire().filter(company_id=company).order_by("pk")


def _has_pvd_plan(company):
    return pvd_plans(company).exists()


def save_pvd_plan(company, form):
    plan = form.save(commit=False)
    plan.company_id = company
    plan.save()
    return plan


# ----------------------------------------------------------- step 6: PDPA

NOTICE_VERSION = "1-draft"
DRAFT_MARKER_TH = "[ร่าง — ให้ที่ปรึกษากฎหมายตรวจก่อนใช้งาน]"
DRAFT_MARKER_EN = "[DRAFT — have counsel review before use]"

_EMPLOYEE_TH = """{marker}

ประกาศความเป็นส่วนตัวสำหรับพนักงาน

{company} ("บริษัท") เก็บรวบรวม ใช้ และเปิดเผยข้อมูลส่วนบุคคลของพนักงาน เช่น ชื่อ เลขประจำตัวประชาชน ที่อยู่ ข้อมูลบัญชีธนาคาร เงินเดือน การลา และข้อมูลการลดหย่อนภาษี เพื่อบริหารงานบุคคล จ่ายเงินเดือน หักและนำส่งภาษีเงินได้หัก ณ ที่จ่ายและเงินสมทบประกันสังคม ตามสัญญาจ้างและตามที่กฎหมายกำหนด

บริษัทเปิดเผยข้อมูลเท่าที่จำเป็นแก่กรมสรรพากร สำนักงานประกันสังคม ธนาคารที่จ่ายเงินเดือน และผู้จัดการกองทุนสำรองเลี้ยงชีพ (ถ้ามี) และเก็บรักษาข้อมูลตลอดระยะเวลาการจ้างและตามระยะเวลาที่กฎหมายกำหนดหลังจากนั้น

พนักงานมีสิทธิขอเข้าถึง ขอแก้ไข ขอลบ ขอระงับการใช้ คัดค้าน และขอโอนย้ายข้อมูลส่วนบุคคลของตน และถอนความยินยอมได้ทุกเมื่อ ตามพระราชบัญญัติคุ้มครองข้อมูลส่วนบุคคล พ.ศ. 2562

ติดต่อเจ้าหน้าที่คุ้มครองข้อมูลส่วนบุคคล (DPO): {dpo}"""

_EMPLOYEE_EN = """{marker}

Employee privacy notice

{company} ("the company") collects, uses and discloses employees' personal data, such as name, national ID, address, bank account, salary, leave and tax allowance data, to administer employment, pay salaries, and withhold and remit income tax and social security contributions, as the employment contract and the law require.

Data is disclosed only as needed to the Revenue Department, the Social Security Office, the paying bank and the provident fund manager (if any), and is kept for the duration of employment and afterwards for the periods the law requires.

Employees may request access, rectification, erasure, restriction, objection and portability, and may withdraw consent at any time, under the Personal Data Protection Act B.E. 2562 (2019).

Data Protection Officer (DPO): {dpo}"""

_CANDIDATE_TH = """{marker}

ประกาศความเป็นส่วนตัวสำหรับผู้สมัครงาน

{company} ("บริษัท") เก็บรวบรวมและใช้ข้อมูลส่วนบุคคลที่ผู้สมัครให้ไว้ เช่น ชื่อ ข้อมูลการติดต่อ ประวัติการศึกษาและการทำงาน เพื่อพิจารณาการสมัครงาน นัดสัมภาษณ์ และติดต่อกลับ

หากผู้สมัครไม่ได้รับการคัดเลือก บริษัทจะเก็บข้อมูลไว้ไม่เกินระยะเวลาที่กำหนดในนโยบายการเก็บรักษาข้อมูล และจะเก็บไว้นานกว่านั้นเพื่อพิจารณาตำแหน่งอื่นเฉพาะเมื่อได้รับความยินยอม

ผู้สมัครมีสิทธิตามพระราชบัญญัติคุ้มครองข้อมูลส่วนบุคคล พ.ศ. 2562 ติดต่อเจ้าหน้าที่คุ้มครองข้อมูลส่วนบุคคล (DPO): {dpo}"""

_CANDIDATE_EN = """{marker}

Candidate privacy notice

{company} ("the company") collects and uses the personal data candidates provide, such as name, contact details, education and work history, to consider applications, arrange interviews and reply.

If a candidate is not selected, the data is kept no longer than the retention policy allows, and longer for other vacancies only with consent.

Candidates have the rights given by the Personal Data Protection Act B.E. 2562 (2019). Data Protection Officer (DPO): {dpo}"""

NOTICE_TEMPLATES = {
    "employee": ("ประกาศความเป็นส่วนตัวสำหรับพนักงาน / Employee privacy notice", _EMPLOYEE_TH, _EMPLOYEE_EN),
    "candidate": ("ประกาศความเป็นส่วนตัวสำหรับผู้สมัครงาน / Candidate privacy notice", _CANDIDATE_TH, _CANDIDATE_EN),
}


def pdpa_settings(company):
    from orvahr_pdpa.models import PdpaSettings

    return PdpaSettings.all_objects.filter(company=company).first()


def company_notices(company):
    from orvahr_pdpa.models import PrivacyNotice

    return PrivacyNotice.all_objects.filter(company=company).order_by("audience", "-effective_from", "-pk")


def is_draft_notice(notice):
    """A notice made from the template still carries the draft marker until someone edits it out."""
    return DRAFT_MARKER_TH in (notice.text_th or "") or DRAFT_MARKER_EN in (notice.text_en or "")


def _pdpa_ready(company):
    row = pdpa_settings(company)
    if not (row and row.dpo_name):
        return False
    return company_notices(company).filter(published=True, audience="employee").exists()


def _legal_name(company):
    from orvahr_filing.models import profile_for

    profile = profile_for(company)
    return (profile.legal_name_th if profile else "") or company.company


def _dpo_line(row):
    parts = [row.dpo_name, row.dpo_email, row.dpo_phone] if row else []
    return ", ".join(p for p in parts if p) or "-"


def save_dpo(company, form):
    row = form.save(commit=False)
    row.company = company
    row.save()
    return row


def publish_notices(company, audiences):
    """
    Publish the template notice for each audience that has no published
    notice of the company's own yet. Returns the audiences published now.
    """
    from orvahr_pdpa.models import PrivacyNotice

    row = pdpa_settings(company)
    published = []
    for audience in audiences:
        if audience not in NOTICE_TEMPLATES:
            continue
        mine = company_notices(company).filter(audience=audience)
        if mine.filter(published=True).exists() or mine.filter(version=NOTICE_VERSION).exists():
            continue
        title, text_th, text_en = NOTICE_TEMPLATES[audience]
        values = {"company": _legal_name(company), "dpo": _dpo_line(row)}
        PrivacyNotice(
            company=company,
            audience=audience,
            version=NOTICE_VERSION,
            title=title,
            text_th=text_th.format(marker=DRAFT_MARKER_TH, **values),
            text_en=text_en.format(marker=DRAFT_MARKER_EN, **values),
            effective_from=timezone.localdate(),
            published=True,
        ).save()
        published.append(audience)
    return published


# --------------------------------------------------------- step 7: people


def employee_count(company, exclude_user=None):
    from employee.models import Employee

    qs = Employee.objects.entire().filter(
        is_active=True, employee_work_info__company_id=company
    )
    if exclude_user is not None and getattr(exclude_user, "pk", None):
        qs = qs.exclude(employee_user_id=exclude_user)
    return qs.count()


THAI_IMPORT_SAMPLE = {
    "Badge ID": "EMP-0001",
    "First Name": "สมชาย",
    "Last Name": "ใจดี",
    "Email": "somchai@example.co.th",
    "Phone": "0812345678",
    "Gender": "male",
    "Address": "99/1 ถนนสุขุมวิท แขวงคลองเตย เขตคลองเตย กรุงเทพมหานคร 10110",
    "Department": "ฝ่ายบัญชี",
    "Job Position": "พนักงานบัญชี",
    "Employee Type": "Full-Time",
    "Location": "กรุงเทพมหานคร",
    "Date Joining": "2026-01-05",
    "Basic Salary": "25000",
    "Bank Name": "ธนาคารกสิกรไทย",
    "Account Number": "0123456789",
    "Bank Branch": "สาขาสีลม",
}

THAI_IMPORT_NOTES = [
    ("คอลัมน์ / Column", "คำอธิบาย / Notes"),
    ("Badge ID", "รหัสพนักงาน ห้ามซ้ำ / Employee code, unique."),
    ("First Name, Last Name", "ชื่อ-นามสกุลภาษาไทยได้ / Thai names are fine."),
    ("Phone", "เบอร์มือถือ 10 หลัก เช่น 0812345678 / 10-digit Thai mobile number."),
    ("Date Joining", "รูปแบบ ค.ศ. YYYY-MM-DD เช่น 2026-01-05 (ไม่ใช่ พ.ศ.) / Christian-era year, not Buddhist-era."),
    ("Basic Salary", "เงินเดือนเป็นบาท ไม่มีจุลภาค / Monthly wage in baht, no commas."),
    ("Company", "เว้นว่างได้ ระบบใช้บริษัทที่เลือกอยู่ / May be left empty: the selected company is used."),
    ("Bank Name, Account Number", "บัญชีรับเงินเดือนของพนักงาน / The employee's salary account."),
    (
        "เลขบัตรประชาชน / National ID",
        "ไม่อยู่ในไฟล์นี้: กรอกในโปรไฟล์ไทยของพนักงานหลังนำเข้า (เลขบัตร เลขประกันสังคม) / "
        "Not in this file: enter national ID and social security number in each employee's Thai profile after import.",
    ),
    ("แถวตัวอย่าง / Sample row", "ลบแถวตัวอย่างก่อนนำเข้า / Delete the sample row before importing."),
]


def thai_import_template(company):
    """The master_import employee template with a Thai sample row and a notes sheet."""
    import pandas as pd
    from openpyxl import load_workbook

    from master_import.datasets import get_dataset
    from master_import.views import _xlsx_bytes

    spec = get_dataset("employees")
    columns = [f.key for f in spec.fields]
    sample = {key: THAI_IMPORT_SAMPLE.get(key, "") for key in columns}
    if "Company" in sample:
        sample["Company"] = company.company
    frame = pd.DataFrame([sample], columns=columns)
    workbook = load_workbook(io.BytesIO(_xlsx_bytes(frame)))
    workbook.active.title = "Employees"
    notes = workbook.create_sheet("คำแนะนำ - Notes")
    for row in THAI_IMPORT_NOTES:
        notes.append(list(row))
    notes.column_dimensions["A"].width = 32
    notes.column_dimensions["B"].width = 110
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# --------------------------------------------------------- step 8: finish


def finish(progress, user=None):
    if progress.completed_at is None:
        progress.completed_at = timezone.now()
    record(progress, "finish", DONE, user=user, current="finish")


# What an accountant (or counsel) has to confirm: every statutory figure the
# wizard loaded is a starting point. (Thai, English) pairs.
ACCOUNTANT_CHECKS = [
    (
        "อัตราเงินสมทบประกันสังคม 5% และฐานค่าจ้าง 1,650–17,500 บาท (ปี 2569–2571) และกำหนดให้หักเฉพาะพนักงานที่ขึ้นทะเบียนประกันสังคมแล้ว",
        _("Social security: the 5% rate and the 1,650-17,500 baht wage base (2026-2028), and that the deduction is assigned only to employees registered with the SSO."),
    ),
    (
        "ชุดภาษีเงินได้บุคคลธรรมดา: อัตราขั้นบันได และค่าใช้จ่าย/ค่าลดหย่อน 160,000 บาท (ค่าลดหย่อนอื่นต้องกรอกรายคน)",
        _("Income tax pack: the progressive bands and the 160,000 baht expense and personal allowance (other allowances are entered per employee)."),
    ),
    (
        "อัตราค่าล่วงเวลาและค่าทำงานในวันหยุด (1.5 / 1 / 2 / 3 เท่า) และการคิดค่าจ้างรายชั่วโมง (30 วัน × 8 ชั่วโมง)",
        _("Overtime and holiday-work multipliers (1.5x / 1x / 2x / 3x) and the hourly rate basis (30 days x 8 hours)."),
    ),
    (
        "จำนวนวันลาแต่ละประเภท (บางค่าเป็นนโยบายบริษัท เช่น ลากิจ 3 วัน ลาอุปสมบท 120 วัน)",
        _("Leave entitlements (some are company-policy placeholders, such as 3 days of personal business leave and 120 days of ordination leave)."),
    ),
    (
        "วันหยุดประจำปีเทียบกับประกาศคณะรัฐมนตรี รวมวันหยุดทางจันทรคติและวันหยุดชดเชย",
        _("Public holidays against the Cabinet announcement, including lunar holidays and substitution days."),
    ),
    (
        "อัตราเงินสะสม/สมทบกองทุนสำรองเลี้ยงชีพ (2–15%) และตารางสิทธิรับเงินสมทบ ตามข้อบังคับกองทุน",
        _("Provident fund contribution rates (2-15%) and the vesting schedule, against the fund's rules."),
    ),
    (
        "เลขประจำตัวผู้เสียภาษี สาขา และเลขที่บัญชีนายจ้างประกันสังคม ตรงกับเอกสารจดทะเบียน",
        _("Tax ID, branch number and SSO employer account against the registration documents."),
    ),
    (
        "ไฟล์โอนเงินเดือนเป็นรูปแบบกลาง (CSV) ไม่ใช่รูปแบบของธนาคาร ตรวจกับธนาคารก่อนใช้",
        _("The salary transfer file is a generic CSV, not a bank's own format: check with the bank before using it."),
    ),
    (
        "ประกาศความเป็นส่วนตัว (ร่าง) ต้องให้ที่ปรึกษากฎหมายตรวจและแก้ไขก่อนใช้งานจริง",
        _("The privacy notice is a draft: have counsel review and edit it before relying on it."),
    ),
]
