from django.apps import AppConfig
from django.db.models.signals import post_migrate


class HorillaAuditConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "horilla_audit"

    def ready(self):
        from django.db.models.signals import post_delete, post_save

        from horilla_audit import registry

        # Register the built-in defaults immediately so audit logging works
        # even before any database I/O. The post_migrate hook below will
        # re-apply once user configuration is readable.
        registry.apply_audit_configuration()

        from horilla.tenancy import is_multi_tenant

        if is_multi_tenant():
            # Per-tenant tracked models/fields on top of the process-wide
            # auditlog registry (see registry.install_tenant_audit).
            registry.install_tenant_audit()

        def _reapply_after_migrate(sender, **kwargs):
            from horilla.tenancy import app_has_tables

            if not app_has_tables("horilla_audit"):
                return
            if is_multi_tenant():
                registry.tenant_audit_targets().invalidate()
            else:
                registry.apply_audit_configuration()

            from horilla_audit.models import HistoryTrackingFields

            try:
                HistoryTrackingFields.seed_default_row()
            except Exception:  # pragma: no cover - table may not exist yet
                import logging

                logging.getLogger(__name__).debug(
                    "Skipping HistoryTrackingFields default seed", exc_info=True
                )

        post_migrate.connect(_reapply_after_migrate, sender=self)

        # Reapply on config changes so the UI takes effect immediately.
        from horilla_audit.models import AuditModelConfig

        post_save.connect(registry.on_config_change, sender=AuditModelConfig)
        post_delete.connect(registry.on_config_change, sender=AuditModelConfig)
