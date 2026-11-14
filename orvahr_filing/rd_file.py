"""
Withholding-tax data file for ภ.ง.ด.1 / ภ.ง.ด.1ก in the Revenue Department's
central format.

Specification (the only source for this layout):
    กรมสรรพากร, "รูปแบบข้อมูล (FORMAT) ภ.ง.ด.1 ภ.ง.ด.1ก และ ภ.ง.ด.1ก พิเศษ",
    Version 2.0, ปรับปรุง ณ วันที่ 16/06/2568 (16 June 2025).
    https://www.rd.go.th/fileadmin/user_upload/WHT/Download/FormatPND1V2_0.pdf
    (listed at https://www.rd.go.th/63724.html), checked against the RD's
    sample files https://www.rd.go.th/fileadmin/user_upload/WHT/Download/swc_Data_Test.zip
    (PND1_..._2555_04_00_00.txt, PND1A_..._2555_00_00_00.txt).

What the spec defines and this follows:
    * file name TAX_TYPE_NID(13)_BRANCH(6)_TAXYEAR(BE)_TAXMONTH(2)_FORMTYPE(2)_SEND(2).txt
    * one H (header, 22 fields) record, then D (detail, 26 fields) records
    * fields separated by "|", no leading / trailing "|", empty field = "||"
    * UTF-8 (the RD samples carry a BOM, so this does too), CR/LF line ends
    * no padding; branch numbers left-zero-padded to 6 digits
    * amounts with two decimals, "0.00" when nothing; dates DDMMYYYY in BE;
      PND 1 Kor's paid date is the end of the year
    * characters * + / \\ ! $ % # & @ , ' " are not allowed in the data

How it is used: per the RD's New e-Filing FAQ (efiling.rd.go.th/rd-cms/ask)
a .txt file is NOT uploaded to New e-Filing directly; it is filed through the
"ยื่นด้วยสื่อฯ / ฝากไฟล์" channel or converted with RD Prep.

Choices this module makes where the spec leaves the value to the filer,
listed so they can be checked:
    * TAX_RATE: salary under 40(1) has no single rate; "0.00" is written.
    * PAID_DATE (monthly): the end of the payee's latest payslip period in the
      month (OrvaHR has no payment date).
    * INC_TYPE_PND is always 1 (40(1)) and PAY_CON always 1 (หัก ณ ที่จ่าย),
      see orvahr_filing.pnd1.
    * TIN (old 10-digit ID): always "0000000000", as the spec says when none.
    * Address: OrvaHR keeps one free-text address line; it goes into ADD_NO
      when it fits 40 characters, else STREET_NAME. Disallowed characters are
      replaced by a space, and the line is flagged.
    * SENDER_* = the company itself, SENDER_ROLE 1 (withholding agent),
      SENDER_ID "0000", DEPT_NAME "สำนักงานใหญ่", FORM_TYPE "00" (ordinary
      filing), SUR_AMT "0.00" (the RD computes it), TRANS_AMT = total tax for
      internet filing and "0.00" for media, as the spec describes.
"""

import re
from dataclasses import dataclass, field
from decimal import Decimal

from orvahr_filing import common, pnd1
from orvahr_th import formatting

SPEC_TITLE = "รูปแบบข้อมูล (FORMAT) ภ.ง.ด.1 ภ.ง.ด.1ก และ ภ.ง.ด.1ก พิเศษ"
SPEC_VERSION = "2.0 (16/06/2568)"
SPEC_URL = "https://www.rd.go.th/fileadmin/user_upload/WHT/Download/FormatPND1V2_0.pdf"

BOM = "﻿"
CRLF = "\r\n"
DEPT_HEAD_OFFICE = "สำนักงานใหญ่"
_FORBIDDEN = re.compile(r"[*+/\\!$%#&@,'\"|\r\n\t]")

# Problem codes for this file only.
MISSING_PROFILE = "missing_profile"
MISSING_ADDRESS = "missing_address"
CHARS_REPLACED = "chars_replaced"

TAX_TYPES = {pnd1.PND1: "PND1", pnd1.PND1K: "PND1A"}


class NotReady(Exception):
    """The file cannot be produced; ``problems`` says why."""

    def __init__(self, problems):
        super().__init__("RD file not ready")
        self.problems = problems


@dataclass
class FileProblem:
    code: str
    payee: object = None


@dataclass
class RdFile:
    name: str
    content: bytes
    warnings: list = field(default_factory=list)


def money(value):
    """15,2: two decimals, no grouping, "0.00" when nothing."""
    return f"{Decimal(value or 0).quantize(common.SATANG):f}"


def branch6(branch):
    return (branch or "").zfill(6)


def be_date(date):
    return f"{date.day:02d}{date.month:02d}{date.year + formatting.BE_OFFSET:04d}"


def clean_text(value, limit):
    """(text, changed): disallowed characters replaced, cut to ``limit``."""
    value = (value or "").strip()
    cleaned = re.sub(r"\s+", " ", _FORBIDDEN.sub(" ", value)).strip()
    return cleaned[:limit], cleaned != value or len(cleaned) > limit


def _address_fields(payee):
    line, changed = clean_text(payee.address_line, 100)
    add_no, street = (line, "") if len(line) <= 40 else ("", line)
    return add_no, street, changed


def check(report):
    """Problems that stop the file being produced (empty list: ready)."""
    problems = []
    profile = report.profile
    if profile is None or not profile.tax_id:
        problems.append(FileProblem(MISSING_PROFILE))
    for line in report.blocking_issues:
        for code in line.payee.problems:
            if code in (common.MISSING_TAX_ID, common.INVALID_TAX_ID):
                problems.append(FileProblem(code, line.payee))
    for line in report.lines:
        p = line.payee
        if not (p.district and p.province and re.fullmatch(r"\d{5}", p.postcode or "")):
            problems.append(FileProblem(MISSING_ADDRESS, p))
    return problems


def file_name(report):
    profile = report.profile
    month = f"{report.month:02d}" if report.kind == pnd1.PND1 else "00"
    year = report.year + formatting.BE_OFFSET
    return (
        f"{TAX_TYPES[report.kind]}_{profile.tax_id}_{branch6(profile.branch_no)}"
        f"_{year:04d}_{month}_00_00.txt"
    )


def header(report):
    profile = report.profile
    internet = profile.rd_filing_channel == "2"
    month = f"{report.month:02d}" if report.kind == pnd1.PND1 else "00"
    user_id, _changed = clean_text(profile.rd_user_id, 20)
    fields = [
        "H",
        "0000",
        profile.tax_id,
        branch6(profile.branch_no),
        "1",
        TAX_TYPES[report.kind],
        profile.tax_id,
        branch6(profile.branch_no),
        DEPT_HEAD_OFFICE,
        "1" if profile.rd_large_taxpayer else "0",
        month,
        f"{report.year + formatting.BE_OFFSET:04d}",
        profile.rd_branch_type or "",
        "00",
        str(len(report.lines)),
        money(report.total_income),
        money(report.total_tax),
        money(0),
        money(report.total_tax),
        money(report.total_tax if internet else 0),
        user_id,
        profile.rd_filing_channel or "2",
    ]
    return "|".join(fields)


def detail(report, line):
    payee = line.payee
    profile = report.profile
    if report.kind == pnd1.PND1K:
        paid = be_date(line.paid_date.replace(month=12, day=31))
    else:
        paid = be_date(line.paid_date)
    title, c1 = clean_text(payee.title, 100)
    first, c2 = clean_text(payee.first_name, 100)
    last, c3 = clean_text(payee.last_name, 80)
    add_no, street, c4 = _address_fields(payee)
    tambon, c5 = clean_text(payee.subdistrict, 50)
    amphur, c6 = clean_text(payee.district, 50)
    province, c7 = clean_text(payee.province, 50)
    fields = [
        "D",
        str(line.seq),
        branch6(profile.branch_no),
        payee.tax_id,
        "0000000000",
        title or "-",
        first,
        last,
        paid,
        "0.00",
        money(line.income),
        money(line.tax),
        "1",
        line.condition,
        "",  # BUILD_NAME
        "",  # ROOM_NO
        "",  # FLOOR_NO
        "",  # VILLAGE_NAME
        add_no,
        "",  # MOO_NO
        "",  # SOI
        street,
        tambon,
        amphur,
        province,
        payee.postcode,
    ]
    return "|".join(fields), any((c1, c2, c3, c4, c5, c6, c7))


def build(report):
    """The file for a pnd1.Report; raises NotReady listing what is missing."""
    problems = check(report)
    if problems:
        raise NotReady(problems)
    records = [header(report)]
    warnings = []
    for line in report.lines:
        record, changed = detail(report, line)
        records.append(record)
        if changed:
            warnings.append(FileProblem(CHARS_REPLACED, line.payee))
    text = BOM + CRLF.join(records) + CRLF
    return RdFile(name=file_name(report), content=text.encode("utf-8"), warnings=warnings)


def problem_label(code):
    from django.utils.translation import gettext as _

    labels = {
        MISSING_PROFILE: _("The company has no tax profile with a tax ID"),
        MISSING_ADDRESS: _("Payee address needs a district, province and 5-digit postcode"),
        CHARS_REPLACED: _("Characters the RD format does not allow were replaced"),
    }
    return labels.get(code) or common.problem_label(code)
