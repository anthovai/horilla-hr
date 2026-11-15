"""
horilla/horilla_backends_gcp.py
"""

from django.db import models
from storages.backends.gcloud import GoogleCloudStorage

from horilla import settings
from horilla.tenant_storage import TenantPrefixedLocationMixin


class PrivateMediaStorage(TenantPrefixedLocationMixin, GoogleCloudStorage):
    """
    PrivateMediaStorage
    """

    # SaaS mode: "<location>/<schema>" per tenant (horilla.tenant_storage).
    default_location = settings.env("NAMESPACE", default="private")
    default_acl = "private"
    file_overwrite = False


# To set the private storage globally
models.FileField.storage = PrivateMediaStorage()
models.ImageField.storage = PrivateMediaStorage()
