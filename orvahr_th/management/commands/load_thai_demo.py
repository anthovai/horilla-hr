"""
Turn the generic demo data into a Thai setup so every Thai feature can be
tried end to end.

    python manage.py load_thai_demo [--company ID|all] [--dry-run]

DEMO DATA ONLY. Every identifier this command writes is SYNTHETIC:

* Employee national IDs are ``1-99-99-<7-digit serial>-<check>``: 13 digits
  with a correct mod-11 check digit (so the validators and filing exports
  accept them), but province/district code ``99-99`` does not exist, so they
  can never belong to a real person.
* Company tax IDs are ``0-1055-99-<5-digit serial>-<check>`` (B.E. 2599
  registration year, which has not happened yet).
* SSO employer accounts, bank account numbers and the student-loan reference
  are made-up numbers.

Idempotent: rows are matched on their natural key and never overwritten once
they exist (an HR user's edits survive a re-run). Random choices are seeded
per employee / company, so a re-run picks the same people. ``--dry-run`` does
all the work inside a transaction and rolls it back, so the counts are exact
and nothing is written. The summary prints counts only, never identifiers.
"""

import io
import random
from datetime import date
from decimal import Decimal

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from base.models import Company
from employee.models import (
    Employee,
    EmployeeBankDetails,
    EmployeeTaxClaim,
    EmployeeThaiProfile,
)
from employee.thai_validators import thai_national_id_checksum_digit

SEED = "orvahr-thai-demo"
DEMO_TAG = "ตัวอย่าง — ให้ฝ่ายกฎหมายตรวจ"

# ---------------------------------------------------------------- name data
MALE_FIRST = [
    "สมชาย", "สมศักดิ์", "ประเสริฐ", "วิชัย", "สุรชัย", "ธนากร", "กิตติพงษ์",
    "ณัฐพล", "อนุชา", "ชัยวัฒน์", "พงศกร", "ปิยะ", "ศุภชัย", "วีระพงษ์",
    "ธีรวัฒน์", "จักรพันธ์", "เอกชัย", "อภิสิทธิ์", "นพดล", "สุทธิพงษ์",
]
FEMALE_FIRST = [
    "สมหญิง", "สุนิสา", "กาญจนา", "วิไลวรรณ", "พรทิพย์", "นภัสสร", "ศิริพร",
    "อรอุมา", "ปวีณา", "ชนิดา", "จิราพร", "รัตนา", "สุภาพร", "ธิดารัตน์",
    "กมลชนก", "พิมพ์ชนก", "วรรณภา", "อัญชลี", "เบญจวรรณ", "ณัฐธิดา",
]
LAST_NAMES = [
    "ใจดี", "สุขสวัสดิ์", "ศรีสมบูรณ์", "รุ่งเรือง", "วงศ์สวัสดิ์", "แก้วมณี",
    "ทองดี", "บุญมา", "พรหมมา", "จันทร์เพ็ญ", "สมบัติทอง", "เจริญผล",
    "ศักดิ์สิทธิ์", "มั่นคง", "ประเสริฐศรี", "ชัยมงคล", "อินทร์แก้ว",
    "สายสุวรรณ", "นาคสวัสดิ์", "ธนวัฒน์", "เพชรรัตน์", "พึ่งบุญ",
    "ศรีวงศ์", "กิจเจริญ", "ปัญญาดี",
]
STREETS = [
    "ถนนสุขุมวิท", "ถนนพหลโยธิน", "ถนนรัชดาภิเษก", "ถนนลาดพร้าว", "ถนนเพชรบุรี",
    "ถนนพระราม 9", "ถนนงามวงศ์วาน", "ถนนแจ้งวัฒนะ", "ถนนบางนา-ตราด", "ถนนนิมมานเหมินท์",
]
SSO_HOSPITALS = [
    "โรงพยาบาลราชวิถี", "โรงพยาบาลนพรัตนราชธานี", "โรงพยาบาลกล้วยน้ำไท",
    "โรงพยาบาลเกษมราษฎร์ ประชาชื่น", "โรงพยาบาลบางปะกอก 9 อินเตอร์เนชั่นแนล",
    "โรงพยาบาลนครธน", "โรงพยาบาลพระนั่งเกล้า", "โรงพยาบาลวิภาราม",
]
# Where demo employees live: Bangkok and its neighbours, a few regional hubs.
HOME_PROVINCE_CODES = ["10", "10", "10", "10", "11", "12", "13", "20", "50", "40"]
COMPANY_LEGAL_NAMES = [
    "บริษัท ออร์วา ตัวอย่าง จำกัด",
    "บริษัท ออร์วา ตัวอย่าง เทรดดิ้ง จำกัด",
    "บริษัท ออร์วา ตัวอย่าง เซอร์วิส จำกัด",
]
COMPANY_ADDRESSES = [
    "123 อาคารตัวอย่าง ชั้น 10 ถนนสีลม",
    "456 อาคารตัวอย่างทาวเวอร์ ชั้น 5 ถนนสุขุมวิท",
    "789 อาคารตัวอย่างพลาซ่า ชั้น 3 ถนนพระราม 9",
]
# A Bangkok subdistrict per company address, by DOPA code (Silom, Khlong Toei
# Nuea, Huai Khwang); any Bangkok subdistrict is used if one is missing.
COMPANY_SUBDISTRICTS = ["100402", "103902", "101701"]

PVD_PLAN_NAME = "กองทุนสำรองเลี้ยงชีพ (ตัวอย่าง)"
PVD_VESTING = [(1, 20), (2, 40), (3, 60), (4, 80), (5, 100)]
STUDENT_LOAN_ISSUER = "กองทุนเงินให้กู้ยืมเพื่อการศึกษา (กยศ.) — ตัวอย่าง"
DEMO_NOTICE_VERSION = "demo-1"

EMPLOYEE_NOTICE_TH = f"""[{DEMO_TAG}]

ประกาศความเป็นส่วนตัวสำหรับพนักงาน (ตัวอย่าง)

บริษัทเก็บรวบรวม ใช้ และเปิดเผยข้อมูลส่วนบุคคลของพนักงาน เช่น ชื่อ เลขประจำตัวประชาชน ข้อมูลบัญชีธนาคาร ข้อมูลเงินเดือน ข้อมูลการลา และข้อมูลการลดหย่อนภาษี เพื่อการบริหารงานบุคคล การจ่ายเงินเดือน การหักและนำส่งภาษีเงินได้ หัก ณ ที่จ่าย และเงินสมทบประกันสังคม ตามสัญญาจ้างและตามที่กฎหมายกำหนด

บริษัทเปิดเผยข้อมูลเท่าที่จำเป็นแก่กรมสรรพากร สำนักงานประกันสังคม ธนาคารที่จ่ายเงินเดือน และผู้จัดการกองทุนสำรองเลี้ยงชีพ และเก็บรักษาข้อมูลตลอดระยะเวลาการจ้างและตามระยะเวลาที่กฎหมายกำหนดหลังจากนั้น

พนักงานมีสิทธิขอเข้าถึง ขอแก้ไข ขอลบ ขอระงับการใช้ คัดค้าน และขอโอนย้ายข้อมูลส่วนบุคคลของตน และถอนความยินยอมได้ทุกเมื่อ โดยติดต่อเจ้าหน้าที่คุ้มครองข้อมูลส่วนบุคคล (DPO) ของบริษัท

ข้อความนี้เป็นเพียงตัวอย่างสำหรับทดสอบระบบ ต้องให้ฝ่ายกฎหมายตรวจและแก้ไขก่อนใช้งานจริง"""

EMPLOYEE_NOTICE_EN = """[SAMPLE - FOR LEGAL REVIEW]

Employee privacy notice (sample)

The company collects, uses and discloses employees' personal data, such as name, national ID, bank account, salary, leave and tax allowance data, to administer employment, pay salaries, withhold and remit income tax and social security contributions, as required by the employment contract and by law.

Data is disclosed only as needed to the Revenue Department, the Social Security Office, the paying bank and the provident fund manager, and is kept for the duration of employment and afterwards for the periods the law requires.

Employees may request access, rectification, erasure, restriction, objection and portability, and may withdraw consent at any time, by contacting the company's Data Protection Officer (DPO).

This text is a sample for testing only. It must be reviewed and amended by counsel before real use."""

CANDIDATE_NOTICE_TH = f"""[{DEMO_TAG}]

ประกาศความเป็นส่วนตัวสำหรับผู้สมัครงาน (ตัวอย่าง)

บริษัทเก็บรวบรวมและใช้ข้อมูลส่วนบุคคลที่ผู้สมัครให้ไว้ เช่น ชื่อ ข้อมูลการติดต่อ ประวัติการศึกษาและการทำงาน เพื่อพิจารณาการสมัครงาน นัดสัมภาษณ์ และติดต่อกลับ

หากผู้สมัครไม่ได้รับการคัดเลือก บริษัทจะเก็บข้อมูลไว้ไม่เกินระยะเวลาที่กำหนดในนโยบายการเก็บรักษาข้อมูล และจะเก็บไว้นานกว่านั้นเพื่อพิจารณาตำแหน่งอื่นเฉพาะเมื่อได้รับความยินยอม

ผู้สมัครมีสิทธิตามพระราชบัญญัติคุ้มครองข้อมูลส่วนบุคคล พ.ศ. 2562 และติดต่อเจ้าหน้าที่คุ้มครองข้อมูลส่วนบุคคล (DPO) ของบริษัทได้

ข้อความนี้เป็นเพียงตัวอย่างสำหรับทดสอบระบบ ต้องให้ฝ่ายกฎหมายตรวจและแก้ไขก่อนใช้งานจริง"""

CANDIDATE_NOTICE_EN = """[SAMPLE - FOR LEGAL REVIEW]

Candidate privacy notice (sample)

The company collects and uses the personal data candidates provide, such as name, contact details, education and work history, to consider applications, arrange interviews and reply.

If a candidate is not selected, the data is kept no longer than the retention policy allows, and longer for other vacancies only with consent.

Candidates have the rights given by the Personal Data Protection Act B.E. 2562 and may contact the company's Data Protection Officer (DPO).

This text is a sample for testing only. It must be reviewed and amended by counsel before real use."""

CONSENT_PURPOSES = [
    # audience, code, name, name_th, description
    ("employee", "demo-photos", "Photos in company publicity",
     "ใช้ภาพถ่ายในสื่อประชาสัมพันธ์ของบริษัท",
     f"[{DEMO_TAG}] Use of the employee's photo in newsletters and social media."),
    ("employee", "demo-birthday", "Birthday announcements",
     "ประกาศวันเกิดภายในบริษัท",
     f"[{DEMO_TAG}] Showing the employee's birthday to colleagues."),
    ("employee", "demo-line", "Notifications on LINE",
     "รับการแจ้งเตือนผ่าน LINE",
     f"[{DEMO_TAG}] Sending payslip and leave notifications through LINE."),
    ("candidate", "demo-talent-pool", "Keep application for other vacancies",
     "เก็บใบสมัครไว้พิจารณาตำแหน่งอื่น",
     f"[{DEMO_TAG}] Keeping the application for up to two years for other vacancies."),
]


# ---------------------------------------------------------------- id helpers
def with_check_digit(first_twelve):
    """Append the mod-11 check digit to a 12-digit string."""
    if len(first_twelve) != 12 or not first_twelve.isdigit():
        raise ValueError("expected 12 digits")
    return first_twelve + str(thai_national_id_checksum_digit(first_twelve))


def synthetic_national_id(serial):
    """1-99-99-<serial>-c: valid checksum, non-existent province/district."""
    return with_check_digit(f"19999{serial % 10_000_000:07d}")


def synthetic_company_tax_id(serial):
    """0-1055-99-<serial>-c: juristic-person style, registration year 2599."""
    return with_check_digit(f"0105599{serial % 100_000:05d}")


def rng_for(*parts):
    return random.Random(":".join([SEED, *map(str, parts)]))


class Command(BaseCommand):
    help = (
        "Turn the generic demo data into a Thai setup (currency, tax pack, SSO, "
        "company and employee Thai profiles, tax claims, provident fund, PDPA, "
        "holidays, leave types). Synthetic IDs only; idempotent."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--company", default="all", help="Company ID, or 'all' (default)."
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Do everything in a transaction and roll it back.",
        )

    # ------------------------------------------------------------------ main
    def handle(self, *args, **opts):
        companies = self._companies(opts["company"])
        self.dry = opts["dry_run"]
        self.today = date.today()
        self.year = self.today.year
        self.counts = {}

        with transaction.atomic():
            self.employees = list(
                Employee.objects.filter(
                    is_active=True, employee_work_info__company_id__in=companies
                )
                .select_related("employee_work_info")
                .order_by("pk")
            )
            self._currency(companies, all_companies=opts["company"] == "all")
            self._tax_pack(companies)
            sso = self._sso_deduction()
            self._retire_foreign_statutory()
            self._company_profiles(companies, sso)
            self._bank_accounts(companies)
            self._thai_profiles()
            self._assign_sso(sso)
            members = self._provident_fund(companies)
            self._tax_claims(members)
            self._student_loan()
            self._pdpa(companies)
            self._calendar()
            if self.dry:
                transaction.set_rollback(True)

        self._summary(len(companies))

    def _companies(self, value):
        if str(value).lower() == "all":
            companies = list(Company.objects.order_by("pk"))
        else:
            try:
                companies = [Company.objects.get(pk=int(value))]
            except (ValueError, Company.DoesNotExist):
                raise CommandError("--company must be an existing company ID or 'all'.")
        if not companies:
            raise CommandError("There are no companies to set up.")
        return companies

    def count(self, key, n=1):
        self.counts[key] = self.counts.get(key, 0) + n

    # --------------------------------------------------------------- payroll
    def _currency(self, companies, all_companies):
        from payroll.models.tax_models import PayrollSettings

        targets = []
        for company in companies:
            row = PayrollSettings.objects.filter(company_id=company).first()
            if row is None:
                PayrollSettings(
                    company_id=company, currency_symbol="฿", position="prefix"
                ).save()
                self.count("Payroll currency set to ฿")
            else:
                targets.append(row)
        if all_companies:
            targets += list(PayrollSettings.objects.filter(company_id__isnull=True))
        for row in targets:
            if row.currency_symbol == "฿" and row.position == "prefix":
                self.count("Payroll currency already ฿")
                continue
            row.currency_symbol = "฿"
            row.position = "prefix"
            row.save()
            self.count("Payroll currency set to ฿")

    def _tax_pack(self, companies):
        from payroll.models.models import Contract, FilingStatus
        from payroll.tax_packs import TAX_PACKS, load_tax_packs

        created = load_tax_packs(["th_2026"])
        self.count("Thai tax filing status created", len(created))
        name = next(p["name"] for p in TAX_PACKS if p["key"] == "th_2026")
        status = FilingStatus.objects.filter(filing_status=name).order_by("pk").first()
        if status is None:
            raise CommandError("The Thai tax pack could not be loaded.")
        changed = (
            Contract.objects.filter(
                contract_status="active",
                employee_id__employee_work_info__company_id__in=companies,
            )
            .exclude(filing_status=status)
            .update(filing_status=status)
        )
        self.count("Active contracts given the Thai tax pack", changed)

    def _sso_deduction(self):
        from payroll.component_library import COMPONENT_LIBRARY, load_components
        from payroll.models.models import Deduction

        created = load_components(["th_sso"])
        self.count("SSO deduction created", len(created))
        title = next(e["name"] for e in COMPONENT_LIBRARY if e["key"] == "th_sso")
        return Deduction.objects.filter(title__iexact=title).order_by("pk").first()

    # Statutory deductions of other countries that the generic demo data
    # assigns to everyone. Left active they double the retirement and payroll
    # tax lines on a Thai payslip (India's PF next to the Thai PVD). Switched
    # off, never deleted, so the original demo can be restored by hand.
    FOREIGN_STATUTORY_DEDUCTIONS = ("Provident Fund (PF)", "Professional Tax", "ESI")

    def _retire_foreign_statutory(self):
        from django.db.models import Q

        from payroll.models.models import Deduction

        match = Q()
        for title in self.FOREIGN_STATUTORY_DEDUCTIONS:
            match |= Q(title__iexact=title)
        changed = Deduction.objects.filter(match, is_active=True).update(is_active=False)
        self.count("Non-Thai statutory deductions switched off (PF, Professional Tax, ESI)", changed)

    def _assign_sso(self, sso):
        if sso is None:
            return
        registered = set(
            EmployeeThaiProfile.objects.filter(
                employee_id__in=self.employees, social_security_no__isnull=False
            ).values_list("employee_id", flat=True)
        )
        already = set(sso.specific_employees.values_list("pk", flat=True))
        new = registered - already
        if new:
            sso.specific_employees.add(*new)
        self.count("Employees given the SSO deduction", len(new))

    # ------------------------------------------------------------- companies
    def _company_profiles(self, companies, sso):
        from orvahr_filing.models import CompanyTaxProfile
        from orvahr_geo.models import Subdistrict

        for index, company in enumerate(Company.objects.order_by("pk")):
            if company not in companies:
                continue
            if CompanyTaxProfile.objects.entire().filter(company=company).exists():
                self.count("Company tax profiles already present")
                continue
            slot = index % len(COMPANY_LEGAL_NAMES)
            legal_name = (
                COMPANY_LEGAL_NAMES[index]
                if index < len(COMPANY_LEGAL_NAMES)
                else f"บริษัท ออร์วา ตัวอย่าง {index + 1} จำกัด"
            )
            area = (
                Subdistrict.objects.select_related("district__province")
                .filter(code=COMPANY_SUBDISTRICTS[slot])
                .first()
                or Subdistrict.objects.select_related("district__province")
                .filter(district__province__code="10")
                .order_by("code")
                .first()
            )
            profile = CompanyTaxProfile(
                company=company,
                legal_name_th=legal_name,
                tax_id=synthetic_company_tax_id(company.pk),
                branch_no="00000",
                sso_account_no=f"10{company.pk % 100_000_000:08d}",
                sso_deduction_code=(getattr(sso, "code", "") or "SSO")[:32],
                address_line=COMPANY_ADDRESSES[slot],
                subdistrict=area.name_th if area else "",
                district=area.district.name_th if area else "",
                province=area.district.province.name_th if area else "",
                postcode=area.postcode if area else "",
                phone="02-000-0000",
            )
            profile.full_clean(exclude=["created_by", "modified_by"])
            profile.save()
            self.count("Company tax profiles created")

    def _bank_accounts(self, companies):
        from orvahr_bank.models import CompanyBankAccount

        for company in companies:
            if CompanyBankAccount.objects.entire().filter(company_id=company).exists():
                continue
            CompanyBankAccount(
                company_id=company,
                name="Payroll - KBank (demo)",
                bank_code="004",
                account_number=f"88{company.pk % 100_000_000:08d}",
            ).save()
            self.count("Company payroll bank accounts created")

    # ------------------------------------------------------------- employees
    def _thai_profiles(self):
        from orvahr_geo.models import Subdistrict

        areas = {}
        for sub in Subdistrict.objects.select_related("district__province").filter(
            district__province__code__in=set(HOME_PROVINCE_CODES)
        ).order_by("code"):
            areas.setdefault(sub.district.province.code, []).append(sub)
        all_areas = None
        if not areas:
            all_areas = list(
                Subdistrict.objects.select_related("district__province").order_by("code")[:500]
            )

        existing = {
            p.employee_id_id: p
            for p in EmployeeThaiProfile.objects.filter(employee_id__in=self.employees)
        }
        used_ids = set(
            EmployeeThaiProfile.objects.exclude(national_id__isnull=True).values_list(
                "national_id", flat=True
            )
        )
        bank_codes = [code for code, _label in EmployeeThaiProfile.BANK_CHOICES]
        bank_names = {
            code: str(label) for code, label in EmployeeThaiProfile.BANK_CHOICES
        }
        used_accounts = set(
            EmployeeBankDetails.objects.exclude(account_number__isnull=True).values_list(
                "account_number", flat=True
            )
        )

        created, sso_count, promptpay_count = [], 0, 0
        for employee in self.employees:
            if employee.pk in existing:
                self.count("Employee Thai profiles already present")
                continue
            rng = rng_for("employee", employee.pk)
            gender = employee.gender or rng.choice(["male", "female"])
            if gender == "female":
                title = "นาง" if employee.marital_status == "married" else "นางสาว"
                first = rng.choice(FEMALE_FIRST)
            elif gender == "male":
                title = "นาย"
                first = rng.choice(MALE_FIRST)
            else:
                title = rng.choice(["นาย", "นางสาว"])
                first = rng.choice(MALE_FIRST if title == "นาย" else FEMALE_FIRST)
            last = rng.choice(LAST_NAMES)

            serial = employee.pk
            national_id = synthetic_national_id(serial)
            while national_id in used_ids:
                serial += 1_000_003
                national_id = synthetic_national_id(serial)
            used_ids.add(national_id)

            registered = rng.random() < 0.9
            bank_code = rng.choice(bank_codes)
            promptpay = rng.random() < 0.1

            if all_areas is not None:
                area = rng.choice(all_areas) if all_areas else None
            else:
                pool = areas.get(rng.choice(HOME_PROVINCE_CODES)) or next(iter(areas.values()))
                area = rng.choice(pool)

            profile = EmployeeThaiProfile(
                employee_id=employee,
                name_title=title,
                first_name_th=first,
                last_name_th=last,
                national_id=national_id,
                tax_id=national_id,
                # A Thai employee's SSO number is their national ID.
                social_security_no=national_id if registered else None,
                sso_hospital=rng.choice(SSO_HOSPITALS) if registered else "",
                bank_code=bank_code,
                promptpay_id=national_id if promptpay else None,
                address_line=f"{rng.randint(1, 299)}/{rng.randint(1, 99)} {rng.choice(STREETS)}",
                subdistrict=area.name_th if area else "",
                district=area.district.name_th if area else "",
                province=area.district.province.name_th if area else "",
                postcode=area.postcode if area else "",
            )
            profile.save()
            created.append(employee)
            sso_count += registered
            promptpay_count += promptpay

        if created:
            self.count("Employee Thai profiles created", len(created))
            self.count("  of which registered with SSO", sso_count)
            self.count("  of which with PromptPay", promptpay_count)
        for employee in created:
            profile = employee.thai_profile
            self._bank_details(
                employee, profile.bank_code, bank_names[profile.bank_code], used_accounts
            )

    def _bank_details(self, employee, bank_code, bank_name, used_accounts):
        """Point the employee's bank details at the Thai bank chosen above."""
        bank = EmployeeBankDetails.objects.filter(employee_id=employee).first()
        account = (bank.account_number or "").replace("-", "").replace(" ", "") if bank else ""
        if not (account.isdigit() and 10 <= len(account) <= 15):
            serial = employee.pk
            account = f"99{serial % 100_000_000:08d}"
            while account in used_accounts:
                serial += 10_000_019
                account = f"99{serial % 100_000_000:08d}"
            used_accounts.add(account)
        fields = {
            "bank_name": bank_name[:50],
            "account_number": account,
            "any_other_code1": bank_code,
            "country": "Thailand",
        }
        if bank is None:
            EmployeeBankDetails.objects.create(
                employee_id=employee, branch="สำนักงานใหญ่", **fields
            )
            self.count("Employee bank details created")
        else:
            EmployeeBankDetails.objects.filter(pk=bank.pk).update(**fields)
            self.count("Employee bank details set to a Thai bank")

    # -------------------------------------------------------------- benefits
    def _provident_fund(self, companies):
        """One plan per company, ~40% members, then the payroll sync."""
        from orvahr_benefits.models import (
            EmployeeProvidentFund,
            PVDVestingStep,
            ProvidentFundPlan,
        )
        from orvahr_benefits.services import pvd

        plans = {}
        for company in companies:
            plan = (
                ProvidentFundPlan.objects.entire()
                .filter(company_id=company, name=PVD_PLAN_NAME)
                .first()
            )
            if plan is None:
                plan = ProvidentFundPlan(
                    company_id=company,
                    name=PVD_PLAN_NAME,
                    fund_manager_name="บลจ. ตัวอย่าง จำกัด (Demo Asset Management)",
                    policy_name="Mixed policy (demo)",
                    fund_registration_no="DEMO-PVD-0001",
                    min_employee_rate=Decimal("2.00"),
                    max_employee_rate=Decimal("15.00"),
                    default_employee_rate=Decimal("5.00"),
                    default_employer_rate=Decimal("5.00"),
                    notes=f"[{DEMO_TAG}] Demo plan created by load_thai_demo.",
                )
                plan.full_clean(exclude=["created_by", "modified_by"])
                plan.save()
                self.count("Provident fund plans created")
            else:
                self.count("Provident fund plans already present")
            for years, percent in PVD_VESTING:
                PVDVestingStep.objects.get_or_create(
                    plan=plan,
                    years_of_service=Decimal(years),
                    defaults={"vested_percent": Decimal(percent)},
                )
            plans[company.pk] = plan

        has_fund = set(
            EmployeeProvidentFund.objects.entire()
            .filter(employee__in=self.employees)
            .values_list("employee_id", flat=True)
        )
        members = set(has_fund)
        for employee in self.employees:
            if employee.pk in has_fund:
                continue
            if rng_for("pvd", employee.pk).random() >= 0.4:
                continue
            plan = plans.get(employee.employee_work_info.company_id_id)
            if plan is None:
                continue
            joined = employee.employee_work_info.date_joining or date(self.year, 1, 1)
            joined = min(max(joined, date(2015, 1, 1)), self.today)
            EmployeeProvidentFund(
                employee=employee,
                plan=plan,
                employee_rate=Decimal("5.00"),
                employer_rate=Decimal("5.00"),
                joined_on=joined,
            ).save()
            members.add(employee.pk)
            self.count("Provident fund members enrolled")

        summary = pvd.sync_pvd_deductions()
        self.count("PVD payroll deductions created by sync", summary.get("created", 0))
        self.count("PVD payroll deductions updated by sync", summary.get("updated", 0))
        return members

    def _student_loan(self):
        from orvahr_benefits.models import DeductionOrder
        from orvahr_benefits.services import orders

        if DeductionOrder.objects.entire().filter(
            issuer=STUDENT_LOAN_ISSUER, employee__in=self.employees
        ).exists():
            self.count("Student-loan orders already present")
            return
        candidates = [
            e for e in self.employees
            if not DeductionOrder.objects.entire().filter(employee=e).exists()
        ]
        if not candidates:
            return
        employee = rng_for("student-loan").choice(candidates)
        order = DeductionOrder(
            employee=employee,
            order_type=DeductionOrder.TYPE_STUDENT_LOAN,
            reference_number="DEMO-SL-000001",
            issuer=STUDENT_LOAN_ISSUER,
            amount_per_month=Decimal("1500.00"),
            total_cap=Decimal("60000.00"),
            start_date=self.today.replace(day=1),
            notes=f"[{DEMO_TAG}] Sample order created by load_thai_demo.",
        )
        order.full_clean(exclude=["created_by", "modified_by", "deduction"])
        order.save()
        orders.sync_order(order)
        self.count("Student-loan orders created")

    # ------------------------------------------------------------ tax claims
    def _tax_claims(self, pvd_members):
        from payroll.models.models import Contract

        wages = dict(
            Contract.objects.filter(
                contract_status="active", employee_id__in=self.employees
            ).values_list("employee_id", "wage")
        )
        with_claims = set(
            EmployeeTaxClaim.objects.filter(
                employee_id__in=self.employees, tax_year=self.year
            ).values_list("employee_id", flat=True)
        )
        for employee in self.employees:
            rng = rng_for("tax-claim", employee.pk)
            if rng.random() >= 0.5:
                continue
            if employee.pk in with_claims:
                self.count("Employees with tax claims already present")
                continue
            claims = []
            if employee.marital_status == "married" or rng.random() < 0.3:
                if rng.random() < 0.6:
                    claims.append(("spouse", 1, None))
            children = employee.children if employee.children else rng.choice([0, 0, 1, 2])
            if children:
                claims.append(("child", min(children, 3), None))
            if rng.random() < 0.4:
                claims.append(("parent", rng.choice([1, 2]), None))
            if rng.random() < 0.6:
                claims.append(("life_insurance", None, rng.choice([12000, 18000, 24000, 36000, 50000])))
            if rng.random() < 0.25:
                claims.append(("health_insurance", None, rng.choice([6000, 9000, 12000, 15000])))
            if rng.random() < 0.15:
                claims.append(("home_loan_interest", None, rng.choice([36000, 48000, 72000, 100000])))
            if employee.pk in pvd_members:
                wage = float(wages.get(employee.pk) or 30000)
                claims.append(("provident_fund", None, round(wage * 0.05 * 12, -2) or 1200.0))
            if not claims:
                claims.append(("life_insurance", None, 12000))
            for code, units, amount in claims:
                EmployeeTaxClaim(
                    employee_id=employee,
                    tax_year=self.year,
                    code=code,
                    units=units,
                    amount=float(amount) if amount is not None else None,
                ).save()
                self.count("Tax claims created")
            self.count("Employees given tax claims")

    # ------------------------------------------------------------------ pdpa
    def _pdpa(self, companies):
        from orvahr_pdpa.models import ConsentPurpose, PdpaSettings, PrivacyNotice

        notices = [
            ("employee", "ประกาศความเป็นส่วนตัวสำหรับพนักงาน (ตัวอย่าง) / Employee privacy notice (sample)",
             EMPLOYEE_NOTICE_TH, EMPLOYEE_NOTICE_EN),
            ("candidate", "ประกาศความเป็นส่วนตัวสำหรับผู้สมัครงาน (ตัวอย่าง) / Candidate privacy notice (sample)",
             CANDIDATE_NOTICE_TH, CANDIDATE_NOTICE_EN),
        ]
        for company in companies:
            for audience, title, text_th, text_en in notices:
                present = PrivacyNotice.all_objects.filter(
                    company=company, audience=audience
                ).filter(Q(published=True) | Q(version=DEMO_NOTICE_VERSION))
                if present.exists():
                    self.count("Privacy notices already published")
                    continue
                PrivacyNotice(
                    company=company,
                    audience=audience,
                    version=DEMO_NOTICE_VERSION,
                    title=title,
                    text_th=text_th,
                    text_en=text_en,
                    effective_from=date(self.year, 1, 1),
                    published=True,
                ).save()
                self.count("Privacy notices published")
            for audience, code, name, name_th, description in CONSENT_PURPOSES:
                _purpose, created = ConsentPurpose.all_objects.get_or_create(
                    company=company,
                    audience=audience,
                    code=code,
                    defaults={
                        "name": name,
                        "name_th": name_th,
                        "description": description,
                        "optional": True,
                    },
                )
                self.count(
                    "Consent purposes created" if created else "Consent purposes already present"
                )
            settings = PdpaSettings.all_objects.filter(company=company).first()
            if settings is None:
                PdpaSettings(
                    company=company,
                    dpo_name="คุณสมใจ ตัวอย่าง (DPO ตัวอย่าง)",
                    dpo_email="dpo@example.com",
                    dpo_phone="02-000-0000",
                    dpo_address=f"[{DEMO_TAG}] ฝ่ายคุ้มครองข้อมูลส่วนบุคคล 123 ถนนสีลม กรุงเทพมหานคร 10500",
                ).save()
                self.count("PDPA settings (DPO) created")
            elif not settings.dpo_name:
                settings.dpo_name = "คุณสมใจ ตัวอย่าง (DPO ตัวอย่าง)"
                settings.dpo_email = settings.dpo_email or "dpo@example.com"
                settings.dpo_phone = settings.dpo_phone or "02-000-0000"
                settings.save()
                self.count("PDPA settings (DPO) filled in")
            else:
                self.count("PDPA settings already present")

    # -------------------------------------------------------------- calendar
    def _calendar(self):
        """Fixed Thai holidays for this year and Thai leave types (global)."""
        from base.models import Holidays
        from leave.models import LeaveType

        before = Holidays.objects.filter(company_id__isnull=True).count()
        out, err = io.StringIO(), io.StringIO()
        call_command("load_thai_holidays", year=self.year, stdout=out, stderr=err)
        self.count(
            "Thai holidays created",
            Holidays.objects.filter(company_id__isnull=True).count() - before,
        )
        if "conflict" in err.getvalue():
            self.count("Thai holidays skipped (another holiday on the date)", err.getvalue().count("conflict"))
        self.holiday_warning = "variable (lunar) holidays NOT loaded" in err.getvalue()

        before = LeaveType.objects.filter(company_id__isnull=True).count()
        call_command("load_thai_leave_types", stdout=io.StringIO(), stderr=io.StringIO())
        self.count(
            "Thai leave types created",
            LeaveType.objects.filter(company_id__isnull=True).count() - before,
        )

    # --------------------------------------------------------------- summary
    def _summary(self, company_count):
        prefix = "[dry-run, nothing written] " if self.dry else ""
        self.stdout.write(
            f"{prefix}Thai demo setup for {company_count} company(ies), "
            f"{len(self.employees)} active employee(s), tax year {self.year}:"
        )
        for key, value in self.counts.items():
            self.stdout.write(f"  {key}: {value}")
        if getattr(self, "holiday_warning", False):
            self.stdout.write(
                "  Note: lunar holidays (Makha Bucha, Visakha Bucha, ...) are not "
                "loaded; add them with load_thai_holidays --file."
            )
        self.stdout.write(
            self.style.SUCCESS(
                "All IDs and account numbers are synthetic demo values; notice "
                "texts are samples for legal review."
            )
        )
