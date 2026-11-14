"""
Leave lifecycle -> LINE, without touching leave/.

pre_save remembers the stored status; post_save compares. Dispatch is
deferred to transaction.on_commit because LeaveRequest.save() creates the
condition-approval rows *after* super().save() (i.e. after post_save), and
because nothing should be sent for a rolled-back request.
"""

from functools import partial

from django.apps import apps
from django.db import transaction
from django.db.models.signals import post_save, pre_save

from line_integration import dispatch

_PREV = "_line_prev_status"
_SENT = "_line_sent_events"
_DEPTH = "_line_save_depth"


def _remember_status(sender, instance, raw=False, **kwargs):
    if raw:
        return
    depth = instance.__dict__.get(_DEPTH, 0) + 1
    instance.__dict__[_DEPTH] = depth
    # attendance re-saves the instance from inside its post_save; only the
    # outermost save records the "before" status and decides.
    if depth > 1:
        return
    previous = None
    if instance.pk:
        previous = (
            sender._base_manager.filter(pk=instance.pk)
            .values_list("status", flat=True)
            .first()
        )
    instance.__dict__[_PREV] = previous


def _on_saved(sender, instance, created, raw=False, **kwargs):
    if raw:
        return
    depth = instance.__dict__.get(_DEPTH, 0) - 1
    if depth > 0:
        instance.__dict__[_DEPTH] = depth
        return
    instance.__dict__.pop(_DEPTH, None)
    if _PREV not in instance.__dict__:  # no pre_save seen: unknown transition
        return
    previous = instance.__dict__.pop(_PREV)
    status = instance.status

    if created and status == "requested":
        key = ("created",)
        job = partial(dispatch.notify_leave_created, instance.pk)
    elif status in ("approved", "rejected") and (created or previous != status):
        key = ("status", status)
        job = partial(dispatch.notify_leave_status, instance.pk, status)
    else:
        return
    sent = instance.__dict__.setdefault(_SENT, set())
    if key in sent:
        return
    sent.add(key)
    if not dispatch.line_active():
        return
    transaction.on_commit(lambda: dispatch.run_async(job))


def connect():
    if not apps.is_installed("leave"):
        return
    from leave.models import LeaveRequest

    pre_save.connect(_remember_status, sender=LeaveRequest, dispatch_uid="line_leave_pre")
    post_save.connect(_on_saved, sender=LeaveRequest, dispatch_uid="line_leave_post")
