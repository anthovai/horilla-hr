"""
Control-plane jobs. Imported by ``run_scheduler`` only when orvahr_saas is
installed (SaaS mode). ``per_tenant=False``: the job runs ONCE, on the public
schema (``horilla.tenancy.run_job_in_public_schema``), not once per tenant.
"""

from horilla.scheduling import register_job
from orvahr_saas.lifecycle import daily_lifecycle

register_job(
    daily_lifecycle,
    "cron",
    job_id="orvahr_saas_lifecycle",
    per_tenant=False,
    hour=6,
    minute=0,
    misfire_grace_time=3600 * 12,
)
