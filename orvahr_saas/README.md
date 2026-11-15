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
| `MIDDLEWARE[1]` | `orvahr_saas.middleware.PlanGateMiddleware` (plan module gating + employee-limit errors, see "Plans") |
| `SHARED_APPS` (public schema) | django_tenants, orvahr_saas, contenttypes, auth, sessions, messages, staticfiles, sites, django_apscheduler |
| `TENANT_APPS` | every other installed app, **plus** contenttypes/auth/sessions/sites again (each tenant has its own permissions, groups, sessions). Apps that join `INSTALLED_APPS` late (horilla_api adds geofencing/facedetection) are added in `OrvahrSaasConfig.ready()` |
| `PUBLIC_SCHEMA_URLCONF` | `orvahr_saas.public_urls` (landing + plans, `/signup/`, `/console/`, `/billing/`, `/health/`, `/ready/`) |
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

## Plans, limits and module gating

`orvahr_saas.Plan` (public schema): `code`, `name_en`/`name_th`,
`max_employees` (empty = unlimited), `modules` (app labels from
`orvahr_saas.plans.MODULE_CATALOG`: attendance, leave, payroll,
orvahr_benefits, recruitment, onboarding, offboarding, pms, project,
helpdesk, asset), `trial_days`, `is_public`, `sort`. Migration 0003 seeds
three **sample** plans (starter / business / enterprise) — edit them before
selling. Prices are not here: they belong to `orvahr_billing`.

`Tenant.plan` (FK) replaces the free-text `plan_code`, which stays as a
mirror (`create_tenant --plan business` still works: `Tenant.save()`
resolves the code). `Tenant.max_employees` overrides the plan's ceiling.
**A tenant without a plan is unrestricted.**

Inside a tenant (no-op with the flag off, on the public host, or without a plan):

* **Employee ceiling** — `Employee.clean()` calls
  `orvahr_saas.limits.check_employee_limit` (new active employee, or
  re-activation, when active employees ≥ limit). Forms show it as a form error;
  every `Employee.save()` runs `full_clean()`, so the REST API, onboarding
  conversion and un-archive are covered and `PlanGateMiddleware` turns the
  error into a JSON 403 (`code: employee_limit_reached`) or a redirect back
  with an error message. Bulk imports are checked before any account is
  created (`employee.methods.methods.bulk_create_user_import` /
  `bulk_create_employee_import`).
* **Modules** — `horilla.config.get_apps_in_base_dir()` drops modules outside
  the plan from the sidebar; `PlanGateMiddleware.process_view` answers 403
  (friendly page, htmx fragment, or JSON `code: module_not_in_plan` under
  `/api/`) for `/<module>/...`, `/api/[v1/]<module>/...` and any view whose
  code lives in a gated app. Employee, reports, PDPA, settings are never gated.

## Public site and sign-up (public host)

`/` lists public plans (prices from billing: a provider registered with
`orvahr_saas.plans.register_price_provider`, else
`orvahr_billing.pricing.public_prices`, else today's monthly
`orvahr_billing.PlanPrice`; otherwise "Contact us"). `/signup/` stores a
`TenantRequest` (status *pending*, PDPA consent to be contacted + timestamp,
IP) — it never provisions. Anti-spam: honeypot field, signed form timestamp
(< 3 s or forged = bot; bots get the normal "thank you" page and nothing is
stored), 5 POSTs/IP/hour, 60 availability checks/IP/minute
(`/signup/check/?slug=`). Refused sub-domains: invalid, `RESERVED_SLUGS`,
existing tenants/schemas/domains, and slugs held by an open request. Language
switch posts to `/i18n/setlang/` (msgids are English; Thai comes from the
.po files).

## Operator console (`/console/`, public host only)

Operators are `orvahr_saas.ConsoleOperator` rows, **not** tenant users
(HorillaUser lives in tenant schemas; the public schema has no user table):

```bash
ORVAHR_CONSOLE_PASSWORD='...' python manage.py create_console_operator --username ops --email ops@orvahr.example
python manage.py create_console_operator --username ops --deactivate
```

Password policy: AUTH_PASSWORD_VALIDATORS + ≥ 12 characters + 3 character
classes, re-checked at every login. Only `is_staff` + `is_active` operators
get in; 5 failures per IP or username lock for 15 minutes; a password change
or deactivation ends existing sessions. 2FA is not implemented — put the
console behind Cloudflare Access / an IP allow-list as well.

Pages: tenants (status, plan, active employees / limit counted inside each
schema and cached 5 minutes — counts only, never personal data), tenant
detail (domains, change plan + employee override, suspend with reason /
resume), sign-up requests (approve / reject / create tenant), audit trail
(`OperatorAction`: who, when, what, IP).

"Create tenant" runs the `create_tenant` command on a `TenantAwareThread`
(the request page polls the log). The operator types the first admin's
password; it reaches the command through a one-off environment variable
removed as soon as the command returns, and is never stored or logged.
Failed provisioning can be re-approved and retried.

Service functions for other apps (`orvahr_saas.services`):
`suspend_tenant(tenant, reason, *, operator=None, actor_label="system")`,
`resume_tenant(tenant, ...)` (back to trial/active as before),
`change_plan(tenant, plan, max_employees=None, ...)`, `tenant_usage(tenant)`,
`slug_availability(slug)`. All of them write the audit trail.

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

## Phase 2 security / operations work (tenant-safety gaps closed)

_This section is maintained separately from the rest of the README; it
records what changed for each item of the "Not tenant-safe yet" list above._

| Gap | Status | Where |
|---|---|---|
| JWT tenant claim | **Closed** | `horilla_api/tokens.py` |
| Mail automations / PMS bonus automations loaded from one schema | **Closed** | `horilla/tenant_config.py`, `horilla_automations/signals.py`, `pms/signals.py` |
| Audit-log model config process-global | **Closed** | `horilla_audit/registry.py` |
| LDAP settings process-global | **LDAP sign-in disabled in SaaS mode** | `horilla_ldap/apps.py` |
| `BIO_DEVICE_THREADS` keyed by device id | **Closed** (keyed by `(schema, id)`) | `biometric/views.py`, `biometric/cbv/biometric.py` |
| Per-tenant backup | **Closed** (`backup_tenant` / `restore_tenant`) | `orvahr_saas/tenant_backup.py` |
| S3 / GCS media not prefixed | **Closed** | `horilla/tenant_storage.py`, `horilla/horilla_backends*.py`, `horilla/storage_backends.py` |
| Provisioning replays ~340 migrations | **Closed** (template clone: ~120 s → ~5 s) | `orvahr_saas/tenant_template.py`, `create_tenant` |
| `/media/` cacheable by Cloudflare | **Closed** (`Cache-Control: private, no-store`, both modes) | `base/views.protected_media` |

### JWT

Every token minted inside a tenant carries `tenant_schema: <schema>`
(login, refresh — the rotated refresh token and the new access token keep
it). In SaaS mode `SIMPLE_JWT["AUTH_TOKEN_CLASSES"]` is
`horilla_api.tokens.TenantAccessToken`, so *every* `JWTAuthentication`
(DRF default, Swagger fallback, `protected_media`'s bearer check) rejects a
token whose claim is missing or names another schema (401 "Token is not valid
for this organisation"); `/auth/refresh/` and `/auth/logout/` do the same for
refresh tokens. **Deploy note:** API tokens issued before this change have no
claim and are rejected in SaaS mode — mobile clients sign in again once.
Flag off: no claim is added and nothing extra is checked.

### Per-tenant runtime config

`horilla/tenant_config.py` adds `TenantConfigCache` (value loaded lazily
inside the active schema, cached per schema, invalidated when the tenant edits
it; with a shared Redis cache other workers notice within 30 s through a
version key) and `TenantSignalRegistry` (dynamic signal handlers kept per
schema behind one static receiver per signal). In SaaS mode:

* **Mail automations** and **PMS bonus-point automations** no longer connect
  handlers process-wide; each tenant's handlers are built from its own rows
  the first time that schema saves something, all tenants' handlers are live
  at once, and they only ever fire in their own schema. Saving a
  `MailAutomation`, `HorillaMailTemplate` or `BonusPointSetting` invalidates
  just that tenant.
* **Audit log**: auditlog's registry holds the union of the models any tenant
  tracks (never shrinks); a `pre_log` receiver drops entries for models the
  active tenant doesn't track and `get_model_fields` returns the active
  tenant's tracked fields. Nothing is logged on the public schema.
  Caveat: a model *newly* registered in a worker process (first time any
  tenant enables it there) can miss the update entry of the save that loaded
  it; creates and later saves are logged.
* **LDAP**: `django_auth_ldap` isn't installed and its backend is commented
  out, so per-tenant LDAP sign-in would be new code. Instead SaaS mode never
  copies a tenant's `LDAPSettings` (bind DN/password) into the process-wide
  `settings.AUTH_LDAP_*`, a system check (`orvahr_saas.E101`) refuses to
  start if an LDAP backend is in `AUTHENTICATION_BACKENDS`, and the LDAP
  settings page says sign-in is unavailable. The per-tenant settings still
  drive `tenant_command import_ldap_users -s <schema>`.
  (`employee`'s older `import_ldap_users` reads `settings.AUTH_LDAP_*`; it is
  shadowed by `horilla_ldap`'s command of the same name.)

### Fast provisioning: template schema

`create_tenant` clones `_orvahr_template` (a schema migrated once; the leading
underscore can't come from a slug) with django-tenants' `clone_schema()`,
renames the clone's 209 auto-named unique constraints back to Django's names
(pg-clone-schema recreates them as `<table>_<cols>_key`), then runs a **real**
`migrate_schemas -s <schema>` — it applies anything a stale template lacks and
fires post_migrate (seeders) inside the new schema. django-tenants'
`TENANT_CREATION_FAKES_MIGRATIONS` was deliberately not used: faking after a
clone from a stale template records migrations whose tables don't exist.

Measured on PostgreSQL 16 (Apple silicon, same database):

| | wall time |
|---|---|
| `create_tenant --no-template` (replay 343 migrations) | 119.7 s |
| `create_tenant` (clone + migrate check + seed) | 4.2–4.9 s |
| `refresh_tenant_template` (one-off build) | 111.7 s |

The clone was compared with a migrated tenant: identical columns (4004),
constraints incl. names (1895), indexes (2116); every identity sequence's next
value is above its table's max id; same rows except
`leave_employeepastleaverestrict` (1 vs 0 — `leave/migrations/__init__.py`
creates that row whenever the migrations package is imported against an
existing table, as on any install that has run `migrate` twice).

```bash
python manage.py migrate_schemas                      # deploy: public + tenants
python manage.py refresh_tenant_template              # then rebuild the template
python manage.py refresh_tenant_template --check      # exit 1 if missing/stale
python manage.py create_tenant ... [--no-template]
```

A missing template is built on the first `create_tenant`. Rebuilds happen in
`_orvahr_template_new` and are swapped in with one DROP+RENAME transaction.
`deploy/saas/backup/backup.sh` lists every non-system schema, so the template
gets a (tiny) dump too — harmless.

### Per-tenant export / offboarding

```bash
ORVAHR_BACKUP_AGE_RECIPIENTS="age1..." \
python manage.py backup_tenant --slug acme --output /backups/acme-$(date +%F).tar.age

python manage.py restore_tenant /backups/acme-2026-11-15.tar.age \
    --schema acme_check --identity /secure/age-key.txt \
    [--domain acme-check.orvahr.example --status suspended]
```

One `age`-encrypted tar (same recipients as `deploy/saas/backup`): 
`MANIFEST.json` (schema, tenant, versions, table/migration counts, sha256 of
every member), `db.dump` (`pg_dump -Fc --schema=<schema>`) and `media.tar.gz`
(`MEDIA_ROOT/<schema>/`; not included with S3/GCS — export the bucket prefix
`<location>/<schema>/` instead). Without recipients the command refuses unless
`--no-encrypt` is passed. `restore_tenant` verifies checksums, restores into a
**new** schema (directly + `ALTER SCHEMA … RENAME` when the original name is
free; via a scratch database — needs CREATEDB — when it is live), checks
table/migration counts against the manifest, unpacks media into
`MEDIA_ROOT/<new>/`, and with `--domain` registers it as a tenant
(default status **suspended** so the scheduler doesn't mail real employees
from a copy) and migrates it to the current code. It never overwrites an
existing schema or a non-empty media folder. `pg_dump`/`pg_restore`/`psql`
come from PATH or `ORVAHR_PG_BIN_DIR` (`--pg-bin-dir`); they must be at least
the server's major version.

### S3 / GCS media

`PrivateMediaStorage` (all three backends) now uses
`TenantPrefixedLocationMixin`: `location` is `<NAMESPACE>/<schema>` inside a
tenant and exactly `<NAMESPACE>` with the flag off. Stored names stay relative,
so no data migration is needed for new SaaS installs; an adopted single-tenant
bucket needs its objects moved from `<NAMESPACE>/` to
`<NAMESPACE>/<schema>/` (like the local media folder).

### Tests

`orvahr_saas/tests/test_security_jwt.py`,
`test_security_tenant_config.py`, `test_provision_speed.py`,
`test_backup_tenant.py` (need `pg_dump`/`pg_restore`/`psql` on PATH); flag-off:
`horilla_api/tests/test_tenant_claim_flag_off.py`,
`base/tests/test_protected_media_cache_control.py`.

### Still open

* Biometric live-capture threads and in-process `BackgroundScheduler`s still
  live in web workers (now keyed per tenant, but lost on restart).
* Signed values share one SECRET_KEY; `csv_tmp/` is shared; the scheduler runs
  tenants sequentially (unchanged from the list above).
* Automation config changes reach *other* worker processes only through a
  shared cache (Redis) — with the per-process LocMem cache they reload on
  restart, which is how single-tenant mode has always behaved.
