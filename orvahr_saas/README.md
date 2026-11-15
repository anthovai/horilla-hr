# orvahr_saas — schema-per-tenant SaaS mode (Phase 1)

OrvaHR can serve many customers from one deployment, each in its own
PostgreSQL schema (django-tenants), routed by subdomain:
`acme.<base-domain>` → schema `acme`.

**Off by default.** Without `ORVAHR_MULTI_TENANT=1` this app is not installed,
`horilla/settings/saas.py` is never imported, and every helper in
`horilla/tenancy.py` is a no-op. Existing installs see no change.

## Switching it on

```bash
ORVAHR_MULTI_TENANT=1
DATABASE_URL=postgres://user:pass@db:5432/orvahr   # PostgreSQL is mandatory
ORVAHR_SAAS_BASE_DOMAIN=orvahr.example            # optional; adds .orvahr.example to
                                                  # ALLOWED_HOSTS + CSRF_TRUSTED_ORIGINS
                                                  # and serves the landing page there
ORVAHR_SAAS_PUBLIC_HOSTS=localhost                # optional extra public hosts
```

What `saas.py` changes (only then):

| Setting | Value |
|---|---|
| DB engine | `django_tenants.postgresql_backend` |
| `DATABASE_ROUTERS` | `django_tenants.routers.TenantSyncRouter` |
| `MIDDLEWARE[0]` | `orvahr_saas.middleware.TenantMiddleware` (host → schema; unknown host → 404 page; suspended → 403 page; `/health/` `/ready/` answer on any host) |
| `SHARED_APPS` (public schema) | django_tenants, orvahr_saas, contenttypes, auth, sessions, messages, staticfiles, sites, django_apscheduler |
| `TENANT_APPS` | every other installed app, **plus** contenttypes/auth/sessions/sites again (each tenant has its own permissions, groups, sessions). Apps that join `INSTALLED_APPS` late (horilla_api adds geofencing/facedetection) are added in `OrvahrSaasConfig.ready()` |
| `PUBLIC_SCHEMA_URLCONF` | `orvahr_saas.public_urls` (landing placeholder, `/health/`, `/ready/`) |
| `CACHES[*].KEY_FUNCTION` | `django_tenants.cache.make_key` (schema-prefixed keys; Redis also gets `REVERSE_KEY_FUNCTION`) |
| Template cached loader | `django_tenants.template.loaders.cached.Loader` (DB templates are per tenant) |
| `STORAGES["default"]` | `horilla.tenancy.TenantMediaStorage`: files in `MEDIA_ROOT/<schema>/`, URL unchanged `/media/<name>` (served by `protected_media` on the tenant's own host, resolved inside that tenant's folder). S3 setups are left as they are — see "Not tenant-safe yet". |
| static | unchanged |

`manage.py migrate` becomes django-tenants' `migrate_schemas` (public first,
then every tenant). Use it explicitly in deploy scripts:

```bash
python manage.py migrate_schemas --shared          # public schema only
python manage.py migrate_schemas --tenant          # every tenant schema
python manage.py migrate_schemas -s acme           # one schema
python manage.py tenant_command <cmd> -s acme ...  # any command inside a tenant
python manage.py all_tenants_command <cmd> ...     # ... inside every tenant
```

A fresh tenant schema runs all ~340 tenant migrations: about **2 minutes**
of CPU (Django migration-state rendering, not SQL). Phase 2 option: a
pre-migrated template schema + `TENANT_BASE_SCHEMA` /
`TENANT_CREATION_FAKES_MIGRATIONS` cloning.

## Provisioning a customer

```bash
ORVAHR_TENANT_ADMIN_PASSWORD='...' python manage.py create_tenant \
    --slug acme --name "Acme Co., Ltd." --domain acme.orvahr.example \
    --admin-email hr@acme.example --status active [--thai-setup]
```

Creates schema `acme` (hyphens become underscores), runs the tenant
migrations (post_migrate seeds groups, shift days, themes, payroll
components inside it), adds the domain, then inside the schema creates the HQ
company and the administrator (superuser + Employee via the same function as
`createhorillauser`) linked to that company. `--thai-setup` loads Thai
geography, Thai leave types and this year's fixed holidays. `--thai-demo`
also runs `load_thai_demo` (synthetic demo data — never for a real customer;
not run by default). The password is only read from the env var
(`--admin-password-env NAME` to change it) or a TTY prompt, and must pass
`AUTH_PASSWORD_VALIDATORS`. Any failure removes the half-created tenant and
schema. Because the admin already exists, the "initialize database" wizard is
closed on the new tenant.

Locally, use `*.localhost` domains (`acme.localhost:8000` resolves to
127.0.0.1 in browsers) and add `.localhost` to `ALLOWED_HOSTS`.

## Adopting an existing single-tenant database as the first tenant

The current install keeps everything in `public`. Turn it into tenant
`acme` (tested on a throwaway PostgreSQL 16 database):

```bash
# 0. Back up. Stop web + scheduler (nothing may write during this).
pg_dump -Fc "$DATABASE_URL" > before-saas.dump

# 1. Move the data out of public and recreate an empty public.
psql "$DATABASE_URL" <<'SQL'
BEGIN;
ALTER SCHEMA public RENAME TO acme;
CREATE SCHEMA public;
GRANT ALL ON SCHEMA public TO CURRENT_USER;
GRANT USAGE ON SCHEMA public TO PUBLIC;
COMMIT;
SQL
#    Extensions created in the old public schema move with it; recreate any
#    the shared schema needs (CREATE EXTENSION ... SCHEMA public).

# 2. Shared tables in the new public (tenant registry, APScheduler store).
ORVAHR_MULTI_TENANT=1 python manage.py migrate_schemas --shared

# 3. Register the renamed schema (no new schema, no new admin) and apply any
#    pending tenant migrations to it.
ORVAHR_MULTI_TENANT=1 python manage.py create_tenant --adopt-existing-schema \
    --slug acme --name "Acme Co., Ltd." --domain acme.orvahr.example --status active

# 4. Media: move uploads into the tenant's folder.
mkdir -p media/acme && find media -mindepth 1 -maxdepth 1 ! -name acme \
    ! -name '.*' -exec mv {} media/acme/ \;
#    (keep media/.generated_secret_key where it is)
```

Leftovers in schema `acme` are harmless: the old `django_apscheduler_*`
tables (the live ones are now in public) — drop them when convenient.
Rollback: `DROP SCHEMA public CASCADE; ALTER SCHEMA acme RENAME TO public;`
and move the media back (or restore the dump).

## Background work

* **Scheduler** — `run_scheduler` asks `horilla.scheduling.scheduler_target()`
  what to hand APScheduler. In SaaS mode every registered job is scheduled
  as `horilla.tenancy.run_job_for_each_tenant("<module>:<func>")`, which runs
  the job once per tenant with status active/trial inside `tenant_context`
  (sequentially; one tenant failing doesn't stop the others; failures are
  re-raised together as `TenantJobError` for the error listener). Jobs on
  shared data register with `per_tenant=False` (only
  `trim_scheduler_history` today). No job body was edited.
* **Threads** — `horilla.tenancy.TenantAwareThread` captures the tenant when
  the thread is created/started and re-activates it in `run()` (also for
  subclasses that override `run()` or call `Thread.__init__` directly).
  `tenant_aware(fn)` does the same for executors, `on_commit` lambdas and
  in-process `BackgroundScheduler.add_job` (biometric). All ORM-touching
  threads in the code base now use one of the two.
* **Startup / post_migrate** — `horilla.tenancy.app_has_tables(label)` is
  False only on the public schema in SaaS mode. It guards the `ready()` DB
  reads (LDAP settings, mail automations, PMS bonus automations, audit config)
  and the post_migrate seeders (shift days, HRMS groups, themes, payroll
  components, PDPA officer group, audit defaults), plus the DB template
  loader. In SaaS mode `django.setup()` runs **zero** queries.

## Running the SaaS test suite

```bash
# throwaway cluster (any PostgreSQL 14+ works)
initdb -D /tmp/pg-saas -U postgres --auth=trust
pg_ctl -D /tmp/pg-saas -o "-p 55432 -c listen_addresses=127.0.0.1 -c fsync=off" start

ORVAHR_MULTI_TENANT=1 \
DATABASE_URL=postgres://postgres@127.0.0.1:55432/orvahr \
python manage.py test orvahr_saas --keepdb

pg_ctl -D /tmp/pg-saas stop && rm -rf /tmp/pg-saas
```

The first run creates three tenant schemas (~2 min each); `--keepdb` keeps
them so later runs take seconds. Without the env vars the modules skip.

## Not tenant-safe yet (Phase 2 list)

See the Phase 1 report; the short version:

* **JWT** — tokens carry `user_id` only; a token minted on tenant A
  replayed on tenant B's host is rejected today only because
  `CHECK_REVOKE_TOKEN` binds it to A's password hash. Add a schema claim.
* **Process-global config loaded from one schema**: mail automations and
  PMS bonus automations (signal handlers are now schema-tagged so they never
  fire in another tenant, but only the last tenant that edited them has
  handlers loaded), audit-log model config (`horilla_audit` registry),
  LDAP settings (`settings.AUTH_LDAP_*`), `settings.BIO_DEVICE_THREADS`.
* **Google Drive backup** dumps the whole DB + MEDIA_ROOT: refused in SaaS
  mode. Per-tenant backup (`pg_dump -n <schema>` + media folder) is to do.
* **S3 media** (addons.py) is not tenant-prefixed.
* **Biometric live-capture threads** and in-process `BackgroundScheduler`s
  (biometric, backup) live in web workers, are lost on restart and not
  owned by run_scheduler.
* Signed values (`django.core.signing`) share one SECRET_KEY across tenants.
* `csv_tmp/` scratch files are shared (random names, read back immediately).
* Scheduler runs tenants sequentially in one process; a 1-minute job × many
  tenants will overrun its interval.
