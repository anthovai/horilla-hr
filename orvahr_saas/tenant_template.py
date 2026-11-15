"""
Pre-migrated template schema for fast tenant provisioning (SaaS mode).

A new tenant schema used to replay every tenant migration (~340): about two
minutes of CPU, nearly all of it Django rendering migration states, not SQL.
Instead, ``create_tenant`` now copies a schema that has already been migrated:

    _orvahr_template        (TEMPLATE_SCHEMA; never a tenant, no domain)
        |  django-tenants' clone_schema() (pg-clone-schema, DATA mode):
        |  tables, data, sequences (with their current values), indexes,
        |  constraints, defaults, triggers
        v
    acme                    then ``migrate_schemas -s acme`` -- a REAL
                            migrate, not --fake: it applies whatever the
                            template was missing (stale template after an
                            upgrade) and fires post_migrate inside the new
                            schema, so every seeder re-runs there.

Why not ``TENANT_BASE_SCHEMA`` + ``TENANT_CREATION_FAKES_MIGRATIONS``, which
django-tenants 4.0.4 offers? That path fakes the migrations after cloning:
with a template that was not refreshed after an upgrade the new tenant would
be recorded as fully migrated while its tables are missing columns. The real
migrate costs a few seconds and cannot be wrong. It also leaves
``Tenant.save()`` behaviour (and the test suite's tenants) unchanged.

The template name starts with an underscore, which no slug can produce
(``validate_slug`` requires a leading letter), so it can never collide with a
customer. It holds only what migrations and post_migrate seeders create --
no company, user or employee.

Refresh it after every upgrade (``manage.py refresh_tenant_template``; a
deploy runs it right after ``migrate_schemas``). Rebuilding happens in a
staging schema that is swapped in with one transaction (DROP + RENAME). A
clone that is running at that moment holds locks the DROP waits for; one that
starts during the swap fails and is cleaned up like any failed provisioning,
so it can simply be re-run.
"""

from __future__ import annotations

import os

from django.conf import settings
from django.core.management import call_command
from django.db import connection, transaction

DEFAULT_TEMPLATE_SCHEMA = "_orvahr_template"


def template_schema_name() -> str:
    return getattr(settings, "ORVAHR_TENANT_TEMPLATE_SCHEMA", None) or os.environ.get(
        "ORVAHR_TENANT_TEMPLATE_SCHEMA", DEFAULT_TEMPLATE_SCHEMA
    )


def _quote(name: str) -> str:
    return connection.ops.quote_name(name)


def template_exists(schema: str | None = None) -> bool:
    from django_tenants.utils import schema_exists

    return schema_exists(schema or template_schema_name())


def pending_migrations(schema: str) -> list:
    """Migrations not yet applied in ``schema`` (empty = up to date)."""
    from django.db.migrations.executor import MigrationExecutor
    from django_tenants.utils import schema_context

    with schema_context(schema):
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        plan = executor.migration_plan(targets)
    connection.set_schema_to_public()
    return [migration for migration, _backwards in plan]


def build_template(schema: str | None = None, verbosity: int = 1, stdout=None):
    """
    (Re)build the template from scratch: migrate a staging schema, then swap
    it in place of the old template in one transaction.
    """
    schema = schema or template_schema_name()
    staging = f"{schema}_new"[:63]
    connection.set_schema_to_public()
    with connection.cursor() as cursor:
        cursor.execute(f"DROP SCHEMA IF EXISTS {_quote(staging)} CASCADE")
        cursor.execute(f"CREATE SCHEMA {_quote(staging)}")
    try:
        call_command(
            "migrate_schemas",
            schema_name=staging,
            interactive=False,
            verbosity=verbosity,
            **({"stdout": stdout} if stdout is not None else {}),
        )
    except BaseException:
        connection.set_schema_to_public()
        with connection.cursor() as cursor:
            cursor.execute(f"DROP SCHEMA IF EXISTS {_quote(staging)} CASCADE")
        raise
    connection.set_schema_to_public()
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(f"DROP SCHEMA IF EXISTS {_quote(schema)} CASCADE")
        cursor.execute(f"ALTER SCHEMA {_quote(staging)} RENAME TO {_quote(schema)}")


def migrate_template(schema: str | None = None, verbosity: int = 1):
    """Apply pending migrations to the existing template in place."""
    schema = schema or template_schema_name()
    call_command(
        "migrate_schemas", schema_name=schema, interactive=False, verbosity=verbosity
    )
    connection.set_schema_to_public()


def clone_template(new_schema: str, schema: str | None = None):
    """Copy the template (structure + seed data + sequence values) to ``new_schema``."""
    from django_tenants.clone import CloneSchema

    schema = schema or template_schema_name()
    CloneSchema().clone_schema(schema, new_schema, "DATA")
    connection.set_schema_to_public()
    align_constraint_names(schema, new_schema)


def _constraints(cursor, schema):
    cursor.execute(
        """
        SELECT cl.relname, pg_get_constraintdef(co.oid), co.conname
        FROM pg_constraint co
        JOIN pg_class cl ON cl.oid = co.conrelid
        JOIN pg_namespace ns ON ns.oid = cl.relnamespace
        WHERE ns.nspname = %s
        """,
        [schema],
    )
    grouped = {}
    for table, definition, name in cursor.fetchall():
        # FK definitions name the referenced table schema-qualified only
        # when it is outside the search path; normalise both sides.
        definition = definition.replace(f"{schema}.", "")
        grouped.setdefault((table, definition), []).append(name)
    return grouped


def align_constraint_names(template: str, new_schema: str) -> int:
    """
    Give the clone's constraints the template's names.

    pg-clone-schema recreates unique constraints inline, so PostgreSQL names
    them ``<table>_<cols>_key`` instead of Django's ``..._<hash>_uniq``
    (209 of them today; definitions identical). Django finds constraints by
    introspection, but a future migration -- or RunSQL -- that refers to one
    by name would then behave differently on cloned tenants than on migrated
    ones. Renaming a unique/primary-key constraint renames its index too.
    Returns the number of constraints renamed.
    """
    renamed = 0
    with connection.cursor() as cursor:
        wanted = _constraints(cursor, template)
        actual = _constraints(cursor, new_schema)
        for key, names in actual.items():
            target = wanted.get(key)
            if not target or len(target) != 1 or len(names) != 1:
                continue
            if names[0] == target[0]:
                continue
            table = key[0]
            cursor.execute(
                f"ALTER TABLE {_quote(new_schema)}.{_quote(table)} "
                f"RENAME CONSTRAINT {_quote(names[0])} TO {_quote(target[0])}"
            )
            renamed += 1
    return renamed
