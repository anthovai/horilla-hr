"""
horilla/horilla_backends.py
"""

from django.db import models
from storages.backends.s3boto3 import S3Boto3Storage

from horilla.tenant_storage import TenantPrefixedLocationMixin


class PrivateMediaStorage(TenantPrefixedLocationMixin, S3Boto3Storage):
    """
    PrivateMediaStorage
    """

    # SaaS mode: "<location>/<schema>" per tenant (horilla.tenant_storage).
    default_location = "private"
    default_acl = "private"
    file_overwrite = False
    custom_domain = False


models.FileField.storage = PrivateMediaStorage()
models.ImageField.storage = PrivateMediaStorage()
