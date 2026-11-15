"""
Delete the raw employee photos stored by the legacy ``facedetection`` add-on.

Those photos are biometric data kept in plain files with no consent record or
retention rule. Face check-in (orvahr_face) replaces them with encrypted
templates created after explicit consent. The command prints counts only,
never names or paths. In SaaS mode it covers every tenant schema.

    manage.py purge_legacy_face_photos --dry-run
    manage.py purge_legacy_face_photos
"""

from django.apps import apps
from django.core.management.base import BaseCommand

from horilla.tenancy import activate, is_multi_tenant, public_schema_name, use_schema


def purge(dry_run=False):
    """{"records": n, "files": n, "file_errors": n} for the active schema."""
    from facedetection.models import EmployeeFaceDetection

    counts = {"records": 0, "files": 0, "file_errors": 0}
    for row in EmployeeFaceDetection.objects.all().iterator():
        counts["records"] += 1
        name = row.image.name if row.image else ""
        storage = row.image.storage if row.image else None
        if dry_run:
            if name:
                counts["files"] += 1
            continue
        if name:
            try:
                if storage.exists(name):
                    storage.delete(name)
                    counts["files"] += 1
            except Exception:
                counts["file_errors"] += 1
            # The post_delete handler would try the path again; it is gone now.
            row.image = None
        row.delete()
    return counts


class Command(BaseCommand):
    help = "Delete raw face photos stored by the legacy facedetection add-on (counts only)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Count what would be deleted; change nothing.")

    def handle(self, *args, **options):
        if not apps.is_installed("facedetection"):
            self.stdout.write("facedetection is not installed; nothing to purge.")
            return
        dry_run = options["dry_run"]
        totals = {"records": 0, "files": 0, "file_errors": 0}
        for label, tenant in self._targets():
            with activate(tenant):
                counts = purge(dry_run=dry_run)
            for key, value in counts.items():
                totals[key] += value
            if label:
                self.stdout.write(f"{label}: {self._line(counts, dry_run)}")
        self.stdout.write(("Would delete " if dry_run else "Deleted ") + self._line(totals, dry_run))

    @staticmethod
    def _line(counts, dry_run):
        text = f"{counts['records']} photo record(s), {counts['files']} file(s)"
        if not dry_run and counts["file_errors"]:
            text += f", {counts['file_errors']} file(s) could not be deleted"
        return text

    @staticmethod
    def _targets():
        if not is_multi_tenant():
            return [("", None)]
        from django_tenants.utils import get_tenant_model

        model = get_tenant_model()
        with use_schema(public_schema_name()):
            tenants = list(model.objects.exclude(schema_name=public_schema_name()).order_by("schema_name"))
        return [(f"schema {t.schema_name}", t) for t in tenants]
