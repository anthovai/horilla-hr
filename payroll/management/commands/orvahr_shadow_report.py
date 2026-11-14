"""
Compare what payroll actually withheld in a year with what the OrvaHR engine's
cumulative method would have withheld.

    manage.py orvahr_shadow_report --year 2026 [--company ID] [--employee ID]
        [--filing-status NAME] [--format table|csv|json] [--limit N]

READ-ONLY: nothing is written to the database. It is an analysis tool, so it
runs whenever ORVAHR_PAYROLL_ENGINE_URL is set, whatever
ORVAHR_PAYROLL_ENGINE_MODE says; without a URL it refuses.

For each employee with issued (confirmed / paid) payslips in the year and a
contract whose filing status is listed in
ORVAHR_PAYROLL_ENGINE_FILING_STATUSES, the payslips are walked in date order.
Each month the engine is asked for the cumulative withholding with the actual
year-to-date INCOME, the ENGINE'S OWN running withheld total, and the
year-to-date social security, so the engine column shows what the cumulative
method would have taken month by month. The year total compares what the
payslips withheld with the engine's annual tax on the year's total income, so
a partial year (a joiner, a leaver) is compared on the income it actually has.

Money is in baht, 2 decimals. Positive differences mean payroll withheld MORE
than the engine (over-withheld); negative, less (under-withheld).

PRIVACY: employees are identified by badge id and primary key only; no names,
national IDs or tax IDs are printed. The figures are still payroll-sensitive
(they are individual pay and tax): treat the output accordingly and do not
paste it into tickets or chats.
"""

import csv
import io
import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from payroll.methods import orvahr_engine
from payroll.methods.orvahr_ytd import ISSUED_STATUSES

AGREE_BAHT = 1.0


def _satang(baht):
    return round(float(baht) * 100)


def _money(value):
    return f"{value:.2f}"


class Skip(Exception):
    """This employee cannot be analysed; the message is the reason."""


class Command(BaseCommand):
    help = (
        "Read-only: compare tax withheld on issued payslips with the OrvaHR "
        "engine's cumulative withholding. Output is payroll-sensitive."
    )

    def add_arguments(self, parser):
        parser.add_argument("--year", type=int, required=True)
        parser.add_argument("--company", type=int, help="Company id")
        parser.add_argument("--employee", type=int, help="Employee id (pk)")
        parser.add_argument("--filing-status", help="Only this filing status name")
        parser.add_argument(
            "--format", choices=("table", "csv", "json"), default="table"
        )
        parser.add_argument("--limit", type=int, help="Analyse at most N employees")

    def handle(self, *args, **options):
        if not getattr(settings, "ORVAHR_PAYROLL_ENGINE_URL", ""):
            raise CommandError(
                "ORVAHR_PAYROLL_ENGINE_URL is not set: there is no engine to "
                "compare against."
            )
        year = options["year"]
        results = []
        for employee_pk in self.employee_ids(options):
            try:
                results.append(self.analyse(employee_pk, year, options))
            except Skip as exc:
                results.append(self.blank(employee_pk, "skipped", str(exc)))
            except Exception as exc:  # one employee never aborts the run
                results.append(self.blank(employee_pk, "error", str(exc)))
        report = {
            "year": year,
            "currency": "THB",
            "sensitive": "payroll-sensitive: individual pay and tax",
            "summary": self.summarise(results),
            "employees": results,
        }
        render = {"table": self.table, "csv": self.csv, "json": self.json}
        self.stdout.write(render[options["format"]](report))

    # -- selection ---------------------------------------------------------

    def employee_ids(self, options):
        from payroll.models.models import Payslip

        payslips = Payslip.objects.filter(
            status__in=ISSUED_STATUSES, end_date__year=options["year"]
        )
        if options["employee"]:
            payslips = payslips.filter(employee_id_id=options["employee"])
        if options["company"]:
            payslips = payslips.filter(
                employee_id__employee_work_info__company_id_id=options["company"]
            )
        ids = list(
            payslips.order_by("employee_id_id")
            .values_list("employee_id_id", flat=True)
            .distinct()
        )
        return ids[: options["limit"]] if options["limit"] else ids

    def contract_for(self, employee_pk):
        from payroll.models.models import Contract

        contracts = Contract.objects.filter(
            employee_id_id=employee_pk, filing_status__isnull=False
        ).select_related("filing_status")
        return (
            contracts.filter(contract_status="active").order_by("-pk").first()
            or contracts.order_by("-contract_start_date", "-pk").first()
        )

    # -- analysis ----------------------------------------------------------

    def blank(self, employee_pk, status, reason):
        from employee.models import Employee

        badge = (
            Employee.objects.filter(pk=employee_pk)
            .values_list("badge_id", flat=True)
            .first()
        )
        return {
            "employee": employee_pk,
            "badge": badge or "",
            "status": status,
            "reason": reason,
            "months": [],
            "total": None,
        }

    def analyse(self, employee_pk, year, options):
        from payroll.methods.tax_calc import periods_in_year
        from payroll.models.models import Payslip

        contract = self.contract_for(employee_pk)
        if contract is None:
            raise Skip("no filing status")
        filing = contract.filing_status
        enabled = getattr(settings, "ORVAHR_PAYROLL_ENGINE_FILING_STATUSES", [])
        if filing.filing_status not in enabled:
            raise Skip("filing status not enabled for the engine")
        if options["filing_status"] and options["filing_status"] != filing.filing_status:
            raise Skip("filtered out: other filing status")

        payslips = list(
            Payslip.objects.filter(
                employee_id_id=employee_pk,
                status__in=ISSUED_STATUSES,
                end_date__year=year,
            ).order_by("end_date", "pk")
        )
        for payslip in payslips:
            if periods_in_year(contract.pay_frequency, payslip.start_date, payslip.end_date) != 12:
                raise Skip("non-monthly pay")

        claims = orvahr_engine.claims_for(employee_pk, year)
        months = []
        ytd_income = 0.0  # actual
        actual_withheld = 0.0
        engine_withheld = 0.0  # the engine's own running figure
        monthly_sso_sum = 0.0
        prior = 0
        for payslip in payslips:
            data = payslip.pay_head_data if isinstance(payslip.pay_head_data, dict) else {}
            income = float(data.get(filing.based_on) or 0)
            actual = float(data.get("federal_tax") or 0)
            as_of = payslip.end_date.isoformat()
            monthly_sso = (
                orvahr_engine.sso_paid_year_for(employee_pk, income * 12, as_of=as_of) / 12
            )
            result = orvahr_engine.monthly_withholding(
                {
                    "month": payslip.end_date.month,
                    "salary": _satang(income),
                    "other_income": 0,
                    "ytd_income": _satang(ytd_income),
                    "ytd_withheld": _satang(engine_withheld),
                    "ytd_sso": _satang(monthly_sso_sum),
                    "claims": claims,
                    "as_of": as_of,
                }
            )
            engine_month = result["withhold_this_month"] / 100
            ytd_income += income
            actual_withheld += actual
            engine_withheld += engine_month
            monthly_sso_sum += monthly_sso
            prior += 1
            months.append(
                {
                    "month": payslip.end_date.month,
                    "income": round(income, 2),
                    "actual_tax": round(actual, 2),
                    "engine_tax": round(engine_month, 2),
                    "engine_cumulative": round(engine_withheld, 2),
                    "actual_cumulative": round(actual_withheld, 2),
                    "difference": round(actual - engine_month, 2),
                }
            )

        as_of = f"{year}-12-31"
        sso_year = orvahr_engine.sso_paid_year_for(employee_pk, ytd_income, as_of=as_of)
        annual = orvahr_engine.annual_tax(
            ytd_income, claims=claims, sso_paid_year=sso_year, as_of=as_of
        )["tax_baht"]
        difference = actual_withheld - annual
        if abs(difference) <= AGREE_BAHT:
            status = "agree"
        elif difference > 0:
            status = "over"
        else:
            status = "under"
        from employee.models import Employee

        badge = (
            Employee.objects.filter(pk=employee_pk)
            .values_list("badge_id", flat=True)
            .first()
        )
        return {
            "employee": employee_pk,
            "badge": badge or "",
            "status": status,
            "reason": "",
            "filing_status": filing.filing_status,
            "months": months,
            "total": {
                "income": round(ytd_income, 2),
                "actual_tax": round(actual_withheld, 2),
                "engine_annual_tax": round(annual, 2),
                "difference": round(difference, 2),
            },
        }

    def summarise(self, results):
        summary = {
            "employees": len(results),
            "agree": 0,
            "over_withheld": 0,
            "under_withheld": 0,
            "errors": 0,
            "skipped": 0,
            "skipped_reasons": {},
        }
        for row in results:
            if row["status"] == "agree":
                summary["agree"] += 1
            elif row["status"] == "over":
                summary["over_withheld"] += 1
            elif row["status"] == "under":
                summary["under_withheld"] += 1
            elif row["status"] == "error":
                summary["errors"] += 1
            else:
                summary["skipped"] += 1
                reasons = summary["skipped_reasons"]
                reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1
        return summary

    # -- rendering ---------------------------------------------------------

    def rows(self, report):
        """Flat rows: one per month, one total per employee, one per skip/error."""
        for emp in report["employees"]:
            who = {"employee": emp["employee"], "badge": emp["badge"]}
            for m in emp["months"]:
                yield {**who, "row": "month", "period": m["month"], "status": "",
                       "income": _money(m["income"]),
                       "actual_tax": _money(m["actual_tax"]),
                       "engine_tax": _money(m["engine_tax"]),
                       "engine_cumulative": _money(m["engine_cumulative"]),
                       "difference": _money(m["difference"]), "note": ""}
            if emp["total"]:
                t = emp["total"]
                yield {**who, "row": "total", "period": report["year"],
                       "status": emp["status"], "income": _money(t["income"]),
                       "actual_tax": _money(t["actual_tax"]),
                       "engine_tax": _money(t["engine_annual_tax"]),
                       "engine_cumulative": "",
                       "difference": _money(t["difference"]), "note": ""}
            else:
                yield {**who, "row": emp["status"], "period": "", "status": emp["status"],
                       "income": "", "actual_tax": "", "engine_tax": "",
                       "engine_cumulative": "", "difference": "", "note": emp["reason"]}

    COLUMNS = ["employee", "badge", "row", "period", "status", "income",
               "actual_tax", "engine_tax", "engine_cumulative", "difference", "note"]

    def csv(self, report):
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=self.COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(self.rows(report))
        return out.getvalue().rstrip("\n")

    def json(self, report):
        def fmt(value):
            return _money(value) if isinstance(value, float) else value

        def walk(node):
            if isinstance(node, dict):
                return {k: walk(v) for k, v in node.items()}
            if isinstance(node, list):
                return [walk(v) for v in node]
            return fmt(node)

        return json.dumps(walk(report), indent=2)

    def table(self, report):
        rows = list(self.rows(report))
        widths = {
            c: max([len(c)] + [len(str(r[c])) for r in rows]) for c in self.COLUMNS
        }
        line = lambda r: "  ".join(str(r[c]).ljust(widths[c]) for c in self.COLUMNS).rstrip()
        s = report["summary"]
        out = [
            f"OrvaHR shadow report {report['year']} (THB; difference = actual - engine; "
            "positive = over-withheld). PAYROLL-SENSITIVE.",
            line({c: c for c in self.COLUMNS}),
            *[line(r) for r in rows],
            "",
            f"Employees: {s['employees']}  agree (within 1 baht): {s['agree']}  "
            f"over-withheld: {s['over_withheld']}  under-withheld: {s['under_withheld']}  "
            f"errors: {s['errors']}  skipped: {s['skipped']}",
        ]
        for reason, count in sorted(s["skipped_reasons"].items()):
            out.append(f"  skipped - {reason}: {count}")
        return "\n".join(out)
