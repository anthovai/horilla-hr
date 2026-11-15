"""
Private storage for transfer slips uploaded by tenants.

Files live under ``ORVAHR_ACCOUNT_PRIVATE_ROOT`` (default
``<BASE_DIR>/private/orvahr_account``) in one folder per schema, outside
MEDIA_ROOT, and have no URL: ``/media/`` can never serve them. They are
streamed only by ``orvahr_account.views.payment_notice_slip`` (the tenant's
own admins) and ``orvahr_account.services.open_payment_slip`` (operators,
inside ``schema_context``). The folder follows the active schema, so a stored
name resolved in another tenant's context points into that tenant's folder.
"""

import os

from django.conf import settings
from django.core.files.storage import FileSystemStorage

from horilla.tenancy import current_schema, public_schema_name


def private_root():
    configured = getattr(settings, "ORVAHR_ACCOUNT_PRIVATE_ROOT", None)
    if configured:
        return str(configured)
    return os.path.join(str(settings.BASE_DIR), "private", "orvahr_account")


class AccountPrivateStorage(FileSystemStorage):
    def __init__(self, **kwargs):
        kwargs.setdefault("base_url", None)
        super().__init__(**kwargs)

    @property
    def base_location(self):
        if self._location is not None:
            return self._location
        schema = current_schema()
        if schema == public_schema_name():
            raise RuntimeError("Tenant slips are never stored on the public schema.")
        return os.path.join(private_root(), schema or "_single")

    @property
    def location(self):
        return os.path.abspath(self.base_location)

    def url(self, name):  # never served directly
        raise NotImplementedError("Slips are private; use the account views.")

    def deconstruct(self):
        return ("orvahr_account.storage.AccountPrivateStorage", (), {})


private_storage = AccountPrivateStorage()
