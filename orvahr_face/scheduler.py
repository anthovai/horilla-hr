"""
Daily face-template retention job. Registered with horilla.scheduling and
executed by ``manage.py run_scheduler`` (once per tenant in SaaS mode).
"""

import logging

from horilla.scheduling import register_job

logger = logging.getLogger(__name__)


def purge_face_templates():
    from orvahr_face.services import purge_expired_templates

    deleted = purge_expired_templates()
    if deleted:
        logger.info("Face check-in retention: %s template(s) deleted", deleted)


register_job(purge_face_templates, "interval", job_id="orvahr_face_retention", hours=24)
