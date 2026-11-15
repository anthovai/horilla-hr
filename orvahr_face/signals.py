"""
Deletion triggers for face templates:

* biometric consent withdrawn (a ConsentRecord "withdrawn" for the
  ``biometric_face`` purpose, from any screen) -> delete at once;
* employee archived / offboarded (``is_active`` False) -> delete at once.

Deleting the Employee row cascades. Bulk ``.update(is_active=False)`` skips
signals; the daily retention job (orvahr_face.scheduler) catches those.
"""

import logging

from django.apps import apps
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(post_save, sender="employee.Employee", dispatch_uid="orvahr_face_employee_inactive")
def delete_templates_of_inactive_employee(sender, instance, created, **kwargs):
    if created or instance.is_active:
        return
    from orvahr_face.services import delete_templates, templates_qs

    try:
        if templates_qs(instance).exists():
            delete_templates(instance, "archived")
    except Exception:
        logger.exception("Face templates of an archived employee could not be deleted")


def connect_consent_signal():
    if not apps.is_installed("orvahr_pdpa"):
        return
    post_save.connect(
        delete_templates_on_withdrawal,
        sender="orvahr_pdpa.ConsentRecord",
        dispatch_uid="orvahr_face_consent_withdrawn",
    )


def delete_templates_on_withdrawal(sender, instance, created, **kwargs):
    if not created or instance.action != "withdrawn" or instance.employee_id is None:
        return
    from orvahr_face.consent import is_biometric_record
    from orvahr_face.services import delete_templates

    if not is_biometric_record(instance):
        return
    # Errors propagate: a withdrawal that cannot delete the templates must
    # not be recorded as done.
    delete_templates(instance.employee, "consent_withdrawn")
