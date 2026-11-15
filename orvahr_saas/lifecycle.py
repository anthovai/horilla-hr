"""
Trial lifecycle (public schema), run daily by the scheduler and by
``manage.py saas_lifecycle [--date YYYY-MM-DD] [--dry-run]``.

For every tenant in status ``trial`` with a ``trial_ends_on``:

* ends in N days, N within the smallest reminder threshold not yet sent
  (ORVAHR_SAAS_TRIAL_REMINDER_DAYS, default 7/3/1) -> "trial ending" mail to
  the tenant contact and the operators. A missed day still sends the next
  reminder; re-runs never send the same one twice (NotificationLog);
* ``trial_ends_on`` is before the run date -> status ``trial_expired``
  (``services.expire_trial``: audit entry, sign-in + account pages only) and a
  "trial ended" mail.

Nothing is ever deleted. Suspended / active tenants are not touched.
(Kept free of ``suspend_tenant``/``resume_tenant`` names: orvahr_billing's
integration looks those up in this module too.)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.utils import timezone

from horilla.tenancy import is_multi_tenant, public_schema_name, use_schema

logger = logging.getLogger(__name__)


@dataclass
class LifecycleItem:
    tenant: object
    action: str  # "remind" / "expire"
    days_left: int
    threshold: int | None = None
    outcome: str = ""

    def __str__(self):
        extra = f" (threshold {self.threshold})" if self.threshold else ""
        return (
            f"{self.tenant.schema_name}: {self.action}{extra}, "
            f"{self.days_left} day(s) left -> {self.outcome}"
        )


def _reminder_sent(tenant, threshold):
    from orvahr_saas.models import NotificationLog

    key = f"trial_ending:{tenant.pk}:{tenant.trial_ends_on}:{threshold}"
    return NotificationLog.objects.filter(
        dedupe_key=key, status=NotificationLog.STATUS_SENT
    ).exists()


def run_lifecycle(on_date=None, *, dry_run=False):
    """Apply the rules above; returns the list of ``LifecycleItem``."""
    if not is_multi_tenant():
        return []
    from orvahr_saas import conf, notifications, services
    from orvahr_saas.models import Tenant

    on_date = on_date or timezone.localdate()
    thresholds = conf.trial_reminder_days()
    items = []
    with use_schema(public_schema_name()):
        trials = (
            Tenant.objects.filter(
                status=Tenant.STATUS_TRIAL, trial_ends_on__isnull=False
            )
            .exclude(schema_name=public_schema_name())
            .order_by("trial_ends_on", "schema_name")
        )
        for tenant in trials:
            days_left = (tenant.trial_ends_on - on_date).days
            if days_left < 0:
                item = LifecycleItem(tenant, "expire", days_left)
                if dry_run:
                    item.outcome = "would expire"
                else:
                    try:
                        changed = services.expire_trial(tenant, actor_label="lifecycle")
                    except Exception:  # noqa: BLE001 - one tenant must not stop the run
                        logger.exception("Could not expire trial of %s", tenant)
                        item.outcome = "error"
                    else:
                        if changed:
                            tenant.refresh_from_db()
                            notifications.trial_ended(tenant)
                            item.outcome = "expired"
                        else:
                            item.outcome = "unchanged"
                items.append(item)
                continue
            due = [t for t in thresholds if days_left <= t]
            if not due:
                continue
            threshold = min(due)
            item = LifecycleItem(tenant, "remind", days_left, threshold)
            if _reminder_sent(tenant, threshold):
                item.outcome = "already sent"
            elif dry_run:
                item.outcome = "would remind"
            else:
                sent = notifications.trial_ending(tenant, days_left, threshold)
                item.outcome = "reminded" if sent else "no recipient"
            items.append(item)
    return items


def daily_lifecycle():
    """Scheduler entry point (registered with ``per_tenant=False``)."""
    for item in run_lifecycle():
        logger.info("saas_lifecycle %s", item)
