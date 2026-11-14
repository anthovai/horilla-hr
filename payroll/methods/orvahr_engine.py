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


def annual_tax(gross_yearly_income, claims=None, sso_paid_year=0.0):
    """
    The engine's yearly tax breakdown for an income in baht.

    Returns the engine's response with amounts converted back to baht under the
    key ``tax_baht`` (the raw satang response is kept as ``raw``).
    """
    raw = _post(
        "/v1/tax/annual",
        {
            "gross_income": round(float(gross_yearly_income) * 100),
            "claims": claims or [],
            "sso_paid_year": round(float(sso_paid_year) * 100),
        },
    )
    return {"tax_baht": raw["tax"] / 100, "raw": raw}


def agrees(existing_tax, engine_tax):
    return abs(float(existing_tax) - float(engine_tax)) <= TOLERANCE_BAHT
