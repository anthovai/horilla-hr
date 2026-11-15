"""
backup_tenant / restore_tenant against a real PostgreSQL (pg_dump/pg_restore
must be on PATH or in ORVAHR_PG_BIN_DIR).

The source tenants are cheap clones of the template schema, so the suite
doesn't pay for extra migrations. Encryption is exercised with a stand-in
``age`` executable (the real one is not a test dependency): it proves the
archive is piped through ``age -r <recipient>`` and decrypted with
``age -d -i <identity>``.
"""

import json
import os
import shutil
import stat
import tarfile
import tempfile
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django_tenants.utils import schema_context, schema_exists

from orvahr_saas import tenant_backup, tenant_template
from orvahr_saas.models import Domain, Tenant
from orvahr_saas.tests import base  # noqa: F401  (skips outside SaaS mode)

FAKE_AGE = """#!/bin/sh
# Stand-in for age: "encrypts" by prefixing a marker, logs its arguments.
out=""; dec=0; in=""
echo "$@" >> "$FAKE_AGE_LOG"
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2;;
    -d) dec=1; shift;;
    -r|-R|-i) shift 2;;
    *) in="$1"; shift;;
  esac
done
if [ "$dec" = 1 ]; then
  head -c 8 "$in" | grep -q FAKEAGE || { echo "not encrypted" >&2; exit 1; }
  tail -c +9 "$in" > "$out"
else
  { printf 'FAKEAGE\\n'; cat; } > "$out"
fi
"""

SOURCE = "bk_src"
SCHEMAS = [SOURCE, "bk_copy", "bk_moved", "bk_moved_src", "bk_registered"]


def drop(schema):
    connection.set_schema_to_public()
    with connection.cursor() as cursor:
        cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


class TenantBackupTests(TransactionTestCase):
    serialized_rollback = False

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not shutil.which("pg_dump"):
            raise cls.failureException(
                "pg_dump/pg_restore must be on PATH for the backup tests"
            )
        connection.set_schema_to_public()
        template = tenant_template.template_schema_name()
        if not tenant_template.template_exists(template):
            tenant_template.build_template(template, verbosity=0)
        for schema in SCHEMAS:
            drop(schema)
        cls.media = tempfile.mkdtemp()
        cls.tools = tempfile.mkdtemp()
        cls.age_log = os.path.join(cls.tools, "age.log")
        age = os.path.join(cls.tools, "age")
        with open(age, "w") as fh:
            fh.write(FAKE_AGE)
        os.chmod(age, os.stat(age).st_mode | stat.S_IEXEC)
        cls.out = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        connection.set_schema_to_public()
        for tenant in Tenant.objects.filter(schema_name="bk_registered"):
            Domain.objects.filter(tenant=tenant).delete()
            tenant.delete(force_drop=True)
        for schema in SCHEMAS:
            drop(schema)
        for path in (cls.media, cls.tools, cls.out):
            shutil.rmtree(path, ignore_errors=True)
        super().tearDownClass()

    def _fixture_teardown(self):
        pass

    def setUp(self):
        self.settings_override = override_settings(MEDIA_ROOT=self.media)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.env = mock.patch.dict(
            os.environ,
            {"PATH": f"{self.tools}:{os.environ['PATH']}", "FAKE_AGE_LOG": self.age_log},
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def _make_source(self, schema):
        """A tenant-like schema with one company, one employee row and a file."""
        if not schema_exists(schema):
            tenant_template.clone_template(schema)
        with schema_context(schema):
            from base.models import Company

            Company.objects.get_or_create(company=f"Company of {schema}", hq=True)
        folder = os.path.join(self.media, schema, "base", "icon")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "logo.png"), "wb") as fh:
            fh.write(b"logo-of-" + schema.encode())

    def _backup(self, schema, name, **extra):
        output = os.path.join(self.out, name)
        call_command(
            "backup_tenant", schema=schema, output=output, stdout=StringIO(), **extra
        )
        return output

    def _restore(self, archive, schema, **extra):
        out = StringIO()
        call_command("restore_tenant", archive, schema=schema, stdout=out, **extra)
        connection.set_schema_to_public()
        return out.getvalue()

    def _assert_restored(self, schema, source):
        self.assertTrue(schema_exists(schema))
        self.assertEqual(
            tenant_backup.schema_stats(schema), tenant_backup.schema_stats(
                tenant_template.template_schema_name()
            ),
        )
        self.assertEqual(tenant_template.pending_migrations(schema), [])
        with schema_context(schema):
            from base.models import Company

            company = Company.objects.get()
            self.assertEqual(company.company, f"Company of {source}")
            # Sequences came along: a new row does not collide.
            newer = Company.objects.create(company="After restore", hq=False)
            self.assertGreater(newer.pk, company.pk)
        with open(os.path.join(self.media, schema, "base", "icon", "logo.png"), "rb") as fh:
            self.assertEqual(fh.read(), b"logo-of-" + source.encode())

    # ------------------------------------------------------------------
    def test_refuses_to_write_unencrypted_without_flag(self):
        self._make_source(SOURCE)
        with mock.patch.dict(os.environ, {tenant_backup.RECIPIENTS_ENV: ""}):
            with self.assertRaisesMessage(CommandError, "refusing to write unencrypted"):
                self._backup(SOURCE, "refused.tar")
        self.assertFalse(os.path.exists(os.path.join(self.out, "refused.tar")))

    def test_encrypted_backup_restores_into_new_schema_while_source_is_live(self):
        self._make_source(SOURCE)
        archive = self._backup(SOURCE, "src.tar.age", recipient=["age1testrecipient"])
        with open(archive, "rb") as fh:
            self.assertEqual(fh.read(8), b"FAKEAGE\n", "archive went through age")
        self.assertEqual(stat.S_IMODE(os.stat(archive).st_mode), 0o600)
        with open(self.age_log) as fh:
            self.assertIn("-r age1testrecipient", fh.read())

        with self.assertRaisesMessage(CommandError, "--identity"):
            self._restore(archive, "bk_copy")

        identity = os.path.join(self.tools, "identity.txt")
        open(identity, "w").close()
        output = self._restore(archive, "bk_copy", identity=identity)
        self.assertIn("Restored 'bk_src'", output)
        self._assert_restored("bk_copy", SOURCE)
        # The live source is untouched.
        with schema_context(SOURCE):
            from base.models import Company

            self.assertEqual(Company.objects.count(), 1)

    def test_offboarded_schema_restores_in_place_and_detects_tampering(self):
        self._make_source("bk_moved_src")
        archive = self._backup("bk_moved_src", "moved.tar", no_encrypt=True)
        with tarfile.open(archive) as tar:
            manifest = json.load(tar.extractfile(tenant_backup.MANIFEST))
        self.assertEqual(manifest["schema"], "bk_moved_src")
        self.assertIn("1 file(s)", manifest["media"])
        self.assertFalse(manifest["encrypted"])

        # Offboarding: the customer's schema is gone; restore it elsewhere.
        drop("bk_moved_src")
        self._restore(archive, "bk_moved")
        self._assert_restored("bk_moved", "bk_moved_src")
        self.assertFalse(schema_exists("bk_moved_src"))

        # A modified dump is refused before anything is restored.
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        with tarfile.open(archive) as tar:
            tar.extractall(work, filter="data")
        with open(os.path.join(work, tenant_backup.DB_MEMBER), "ab") as fh:
            fh.write(b"tampered")
        tampered = os.path.join(self.out, "tampered.tar")
        with tarfile.open(tampered, "w") as tar:
            for name in (tenant_backup.MANIFEST, tenant_backup.DB_MEMBER, tenant_backup.MEDIA_MEMBER):
                tar.add(os.path.join(work, name), arcname=name)
        with self.assertRaisesMessage(CommandError, "Checksum mismatch"):
            self._restore(tampered, "bk_tampered")
        self.assertFalse(schema_exists("bk_tampered"))

    def test_restore_can_register_the_copy_as_a_suspended_tenant(self):
        self._make_source(SOURCE)
        archive = self._backup(SOURCE, "reg.tar", no_encrypt=True, no_media=True)
        self._restore(
            archive, "bk_registered", domain="bk-registered.saas.test", no_media=True
        )
        tenant = Tenant.objects.get(schema_name="bk_registered")
        self.assertEqual(tenant.status, Tenant.STATUS_SUSPENDED)
        self.assertEqual(tenant.get_primary_domain().domain, "bk-registered.saas.test")

    def test_existing_schema_is_never_overwritten(self):
        self._make_source(SOURCE)
        archive = self._backup(SOURCE, "dup.tar", no_encrypt=True)
        with self.assertRaisesMessage(CommandError, "already exists"):
            self._restore(archive, SOURCE)
