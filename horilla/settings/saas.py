"""
Multi-tenant (SaaS) settings: one PostgreSQL schema per customer.

Applied by ``horilla/settings/__init__.py`` *after* base, addons and
local_settings, and only when ``ORVAHR_MULTI_TENANT`` is true. With the flag
off (the default) this module is never imported and nothing changes.

    ORVAHR_MULTI_TENANT=1
    DATABASE_URL=postgres://user:pass@host:5432/orvahr     # PostgreSQL only
    ORVAHR_SAAS_BASE_DOMAIN=orvahr.example                 # optional

Request routing: ``TenantMainMiddleware`` (our subclass in orvahr_saas) looks
the request host up in ``orvahr_saas.Domain`` and points the connection's
search_path at that tenant's schema. ``acme.orvahr.example`` -> schema
``acme``. The bare base domain and hosts in ORVAHR_SAAS_PUBLIC_HOSTS get the
public urlconf (landing page + health).
"""

from django.core.exceptions import ImproperlyConfigured

# Apps whose tables live in the public schema. Kept minimal: the tenant
# registry, APScheduler's job store (one scheduler process serves all
# tenants) and the contrib apps every schema needs. contenttypes/auth/
# sessions/sites are ALSO tenant apps (see below), so each tenant gets its
# own permissions, groups and sessions; search_path is "<tenant>, public",
# so inside a tenant its own copy always wins.
SAAS_SHARED_APPS = [
    "django_tenants",
    "orvahr_saas",
    "orvahr_billing",
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "django_apscheduler",
]

# Installed apps that must NOT get tables in tenant schemas.
SAAS_PUBLIC_ONLY_APPS = {
    "django_tenants",
    "orvahr_saas",
    "orvahr_billing",
    "django_apscheduler",
}

_POSTGRES_ENGINES = {
    "django.db.backends.postgresql",
    "django.db.backends.postgresql_psycopg2",
    "django_tenants.postgresql_backend",
}


def apply_multi_tenant(ns):
    """Rewrite the settings namespace ``ns`` (the settings module globals)."""
    env = ns["env"]

    # ---------------------------------------------------------- database
    databases = ns["DATABASES"]
    default = dict(databases["default"])
    if default.get("ENGINE") not in _POSTGRES_ENGINES:
        raise ImproperlyConfigured(
            "ORVAHR_MULTI_TENANT needs PostgreSQL (schema per tenant); "
            f"DATABASES['default'] uses {default.get('ENGINE')!r}. "
            "Set DATABASE_URL=postgres://..."
        )
    default["ENGINE"] = "django_tenants.postgresql_backend"
    # The SQLite-only "timeout" option is not a libpq parameter.
    options = dict(default.get("OPTIONS") or {})
    options.pop("timeout", None)
    default["OPTIONS"] = options
    ns["DATABASES"] = {**databases, "default": default}
    ns["DATABASE_ROUTERS"] = ["django_tenants.routers.TenantSyncRouter"]

    # -------------------------------------------------------------- apps
    installed = [
        app
        for app in ns["INSTALLED_APPS"]
        if app not in ("django_tenants", "orvahr_saas", "orvahr_billing")
    ]
    # orvahr_saas BEFORE django_tenants: Django lets the earlier app win when
    # two apps ship a management command of the same name, and both ship
    # create_tenant -- ours also provisions the company and admin user.
    ns["INSTALLED_APPS"] = ["orvahr_saas", "django_tenants", "orvahr_billing", *installed]
    ns["SHARED_APPS"] = list(SAAS_SHARED_APPS)
    ns["TENANT_APPS"] = [app for app in installed if app not in SAAS_PUBLIC_ONLY_APPS]
    ns["TENANT_MODEL"] = "orvahr_saas.Tenant"
    ns["TENANT_DOMAIN_MODEL"] = "orvahr_saas.Domain"
    ns["PUBLIC_SCHEMA_URLCONF"] = "orvahr_saas.public_urls"

    # -------------------------------------------------------- middleware
    middleware = [
        m for m in ns["MIDDLEWARE"] if m != "orvahr_saas.middleware.TenantMiddleware"
    ]
    ns["MIDDLEWARE"] = ["orvahr_saas.middleware.TenantMiddleware", *middleware]

    # ------------------------------------------------------------- hosts
    base_domain = env("ORVAHR_SAAS_BASE_DOMAIN", default="").strip().lstrip(".")
    ns["ORVAHR_SAAS_BASE_DOMAIN"] = base_domain
    public_hosts = env.list("ORVAHR_SAAS_PUBLIC_HOSTS", default=[])
    if base_domain:
        public_hosts = [base_domain, f"www.{base_domain}", *public_hosts]
        allowed = list(ns["ALLOWED_HOSTS"])
        if "*" not in allowed and f".{base_domain}" not in allowed:
            allowed.append(f".{base_domain}")
        ns["ALLOWED_HOSTS"] = allowed
        trusted = list(ns["CSRF_TRUSTED_ORIGINS"])
        for scheme in ("https", "http"):
            origin = f"{scheme}://*.{base_domain}"
            if origin not in trusted:
                trusted.append(origin)
        ns["CSRF_TRUSTED_ORIGINS"] = trusted
    ns["ORVAHR_SAAS_PUBLIC_HOSTS"] = [h.lower() for h in public_hosts if h]

    # ------------------------------------------------------------- cache
    # Every key is prefixed with the active schema (django_tenants.cache).
    # Without this, a value cached by tenant A under "user_42_perms" would be
    # served to tenant B's user 42.
    caches = ns.get("CACHES")
    if caches:
        caches = {alias: dict(conf) for alias, conf in caches.items()}
    else:
        caches = {
            "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
        }
    for conf in caches.values():
        conf["KEY_FUNCTION"] = "django_tenants.cache.make_key"
        if "django_redis" in conf.get("BACKEND", ""):
            conf["REVERSE_KEY_FUNCTION"] = "django_tenants.cache.reverse_key"
    ns["CACHES"] = caches

    # --------------------------------------------------------- templates
    # Django's cached loader keys compiled templates by name only, so a
    # template one tenant customised in horilla_dbtemplate would be served to
    # every tenant from the process cache. django-tenants' cached loader
    # prefixes the key with the schema.
    templates = []
    for conf in ns["TEMPLATES"]:
        conf = {**conf, "OPTIONS": dict(conf.get("OPTIONS", {}))}
        loaders = conf["OPTIONS"].get("loaders")
        if loaders:
            conf["OPTIONS"]["loaders"] = [
                (
                    ("django_tenants.template.loaders.cached.Loader", entry[1])
                    if isinstance(entry, (list, tuple))
                    and entry[0] == "django.template.loaders.cached.Loader"
                    else entry
                )
                for entry in loaders
            ]
        templates.append(conf)
    ns["TEMPLATES"] = templates

    # ------------------------------------------------------------- media
    # Per-tenant folder MEDIA_ROOT/<schema>/ (horilla.tenancy). S3 setups
    # (addons.py) keep their backend: see docs -- not tenant-prefixed yet.
    storages = dict(ns["STORAGES"])
    default_backend = storages.get("default", {}).get("BACKEND", "")
    if default_backend == "django.core.files.storage.FileSystemStorage":
        storages["default"] = {"BACKEND": "horilla.tenancy.TenantMediaStorage"}
    ns["STORAGES"] = storages

    ns["ORVAHR_MULTI_TENANT"] = True
