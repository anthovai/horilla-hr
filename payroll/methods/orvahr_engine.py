"""
Client for the OrvaHR payroll engine (the Rust service in services/payroll-engine).

Payroll can ask the engine for the yearly tax on an income. Three modes, set by
``ORVAHR_PAYROLL_ENGINE_MODE``:

  off     (default) never called; payroll is exactly what it was.
  shadow  called alongside the existing computation; a difference is logged and
          the existing figure is still the one paid. This is how the engine is
          proven on real data before anyone is paid by it.
  active  the engine's figure is the one used. If it cannot answer, tax
          computation fails loudly instead of quietly paying the old number:
          a silent fallback is a payslip that looks normal and is wrong.

The engine speaks integer satang; this module is the only place that converts.
"""

import json
import logging
import urllib.error
import urllib.request

from django.conf import settings

from employee.tax_claims import PER_UNIT_CODES

logger = logging.getLogger(__name__)

MODES = ("off", "shadow", "active")

# A yearly tax within this many baht of the engine's counts as agreeing: the
# old code works in floats and the engine in whole satang.
TOLERANCE_BAHT = 0.5


class EngineUnavailable(Exception):
    """The engine could not give an answer (down, slow, or refused the input)."""


def mode(filing=None):
    """The effective mode for ``filing``; "off" unless configured and listed."""
    configured = str(getattr(settings, "ORVAHR_PAYROLL_ENGINE_MODE", "off")).lower()
    if configured not in MODES:
        logger.error("ORVAHR_PAYROLL_ENGINE_MODE=%r is not one of %s", configured, MODES)
        return "off"
    if configured == "off" or not getattr(settings, "ORVAHR_PAYROLL_ENGINE_URL", ""):
        return "off"
    if filing is not None:
        listed = getattr(settings, "ORVAHR_PAYROLL_ENGINE_FILING_STATUSES", [])
        if filing.filing_status not in listed:
            return "off"
    return configured


def _post(path, payload):
    url = settings.ORVAHR_PAYROLL_ENGINE_URL.rstrip("/") + path
    if not url.lower().startswith(("http://", "https://")):
        raise EngineUnavailable(f"engine URL must be http(s): {url!r}")
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    timeout = float(getattr(settings, "ORVAHR_PAYROLL_ENGINE_TIMEOUT", 2.0))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read()).get("error", "")
        except Exception:
            pass
        raise EngineUnavailable(f"engine rejected the request ({exc.code}): {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise EngineUnavailable(f"engine not reachable: {exc}") from exc


def as_of_for_year(tax_year):
    """
    The date that selects the engine's rule set for a tax year (its last day),
    or None to let the engine use today. Without it a recalculation of an
    earlier year would quietly use whichever rules are in force now.
    """
    return None if tax_year is None else f"{int(tax_year)}-12-31"


def annual_tax(gross_yearly_income, claims=None, sso_paid_year=0.0, as_of=None):
    """
    The engine's yearly tax breakdown for an income in baht.

    Returns the engine's response with amounts converted back to baht under the
    key ``tax_baht`` (the raw satang response is kept as ``raw``).
    """
    payload = {
        "gross_income": round(float(gross_yearly_income) * 100),
        "claims": claims or [],
        "sso_paid_year": round(float(sso_paid_year) * 100),
    }
    if as_of:
        payload["as_of"] = as_of
    raw = _post("/v1/tax/annual", payload)
    return {"tax_baht": raw["tax"] / 100, "raw": raw}


def _employee_pk(employee):
    """The primary key of an Employee, or an id passed in its place."""
    return getattr(employee, "pk", employee)


def claims_for(employee, year):
    """
    The engine's claim list for ``employee`` in tax year ``year``.

    Reads the employee's EmployeeTaxClaim rows and converts baht to satang:
    per-person codes go as ``units``, capped codes as ``amount`` in satang.
    ``employee`` may be an Employee or its id. Nothing here validates a figure
    against the rules; the engine does (it rejects unknown codes).
    """
    from employee.models import EmployeeTaxClaim

    claims = []
    for row in EmployeeTaxClaim.objects.filter(
        employee_id=_employee_pk(employee), tax_year=year
    ).order_by("code"):
        if row.code in PER_UNIT_CODES:
            claims.append({"code": row.code, "units": int(row.units)})
        else:
            claims.append({"code": row.code, "amount": round(float(row.amount) * 100)})
    return claims


def sso_paid_year_for(employee, gross_yearly_income, as_of=None):
    """
    Projected yearly employee social security in baht, or 0.0 if not applicable.

    Applies only to an employee whose Thai profile carries a social security
    number (taken as "enrolled"). The monthly wage is taken as the yearly
    income / 12 (an approximation: the income given to payroll tax is not
    always the SSO wage) and the engine's monthly employee contribution is
    multiplied by 12. May raise EngineUnavailable.
    """
    from employee.models import EmployeeThaiProfile

    enrolled = (
        EmployeeThaiProfile.objects.filter(employee_id=_employee_pk(employee))
        .exclude(social_security_no__isnull=True)
        .exclude(social_security_no="")
        .exists()
    )
    if not enrolled:
        return 0.0
    wage = round(float(gross_yearly_income) / 12 * 100)
    payload = {"wage": max(wage, 0)}
    if as_of:
        payload["as_of"] = as_of
    raw = _post("/v1/sso", payload)
    return raw["employee"] * 12 / 100


def agrees(existing_tax, engine_tax):
    return abs(float(existing_tax) - float(engine_tax)) <= TOLERANCE_BAHT


def monthly_withholding(payload):
    """
    The engine's cumulative-method withholding for one month.

    ``payload`` is the engine's WithholdingInput (satang throughout). Returns
    the engine's raw response; may raise EngineUnavailable.
    """
    return _post("/v1/withholding/monthly", payload)
