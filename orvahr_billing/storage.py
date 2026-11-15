"""
Private file storage for billing: transfer slips and the seller logo.

Files live under ``ORVAHR_BILLING_PRIVATE_ROOT`` (default
``<BASE_DIR>/private/orvahr_billing``), outside MEDIA_ROOT, and have no URL:
they are only ever streamed by the operator views after the operator check.
"""

import os

from django.conf import settings
from django.core.files.storage import FileSystemStorage


def private_root():
    configured = getattr(settings, "ORVAHR_BILLING_PRIVATE_ROOT", None)
    if configured:
        return str(configured)
    return os.path.join(str(settings.BASE_DIR), "private", "orvahr_billing")


class PrivateBillingStorage(FileSystemStorage):
    def __init__(self, **kwargs):
        kwargs.setdefault("base_url", None)
        super().__init__(**kwargs)

    @property
    def base_location(self):
        return self._location if self._location is not None else private_root()

    @property
    def location(self):
        return os.path.abspath(self.base_location)

    def url(self, name):  # never served directly
        raise NotImplementedError("Billing files are private; use the operator views.")

    def deconstruct(self):
        # Migrations reference the shared instance, not a path baked in at
        # makemigrations time.
        return ("orvahr_billing.storage.PrivateBillingStorage", (), {})


private_storage = PrivateBillingStorage()
