"""
Per-tenant key prefix for the cloud media backends (S3 / GCS).

In SaaS mode local media already goes to ``MEDIA_ROOT/<schema>/``
(``horilla.tenancy.TenantMediaStorage``). The cloud backends in
``horilla/horilla_backends*.py`` write every tenant into one bucket under one
``location`` (``NAMESPACE``), so two tenants uploading
``employee/profile/1.png`` would overwrite and read each other's object.

``TenantPrefixedLocationMixin`` makes ``location`` a property:

* flag off (or public schema): ``location`` is exactly the configured value,
  as before -- e.g. ``private``;
* SaaS mode inside a tenant: ``<configured>/<schema>`` -- e.g.
  ``private/acme`` -- evaluated on every call, so one storage instance (they
  are module-level singletons) serves every tenant correctly.

django-storages reads ``self.location`` for every key it builds
(``_normalize_name``), so stored names stay relative ("employee/1.png") and
never contain the schema; a name copied into another tenant resolves inside
that tenant's prefix.

Moving an existing single-tenant bucket into SaaS mode means moving its
objects from ``<location>/`` to ``<location>/<schema>/`` (``aws s3 mv
--recursive``), just like the local ``media/`` folder (README "Adopting").
"""

from horilla.tenancy import current_schema, public_schema_name


class TenantPrefixedLocationMixin:
    """Put before the storages backend class in the bases."""

    #: Configured location when neither the class nor the settings give one.
    default_location = ""

    @property
    def location(self):
        base = self.__dict__.get("_orvahr_location", self.default_location) or ""
        schema = current_schema()
        if schema is None or schema == public_schema_name():
            return base
        base = base.strip("/")
        return f"{base}/{schema}" if base else schema

    @location.setter
    def location(self, value):
        self.__dict__["_orvahr_location"] = value
