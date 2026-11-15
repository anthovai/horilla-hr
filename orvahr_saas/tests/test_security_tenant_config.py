"""
Runtime configuration that used to be process-global is per tenant in SaaS
mode: mail automations, PMS bonus automations, audit-log models, LDAP,
biometric thread keys and cloud-storage key prefixes.
"""

from django.db import connection
from django.db.models.signals import post_save
from django.test import override_settings

from horilla import tenancy
from orvahr_saas.tests.base import SaasTestCase


class MailAutomationPerTenantTests(SaasTestCase):
    def setUp(self):
        super().setUp()
        from horilla_automations import signals as automation_signals

        automation_signals.start_automation()  # idempotent
        self.registry = automation_signals.tenant_registry()
        self.registry.config.clear()
        self.addCleanup(self.registry.config.clear)

    def _automation(self):
        from horilla_automations.models import MailAutomation

        return MailAutomation.objects.create(
            title="Welcome alpha",
            model="employee.models.Employee",
            mail_to="[]",
            mail_details="employee_id",
            trigger="on_create",
            condition="",
            condition_querystring="",
        )

    def test_handlers_are_loaded_per_schema(self):
        from employee.models import Employee

        self.assertIsNotNone(self.registry)
        self.assertEqual(self.registry.handlers_for(post_save, Employee), ())
        self._automation()  # saved in alpha -> alpha's entry invalidated
        self.assertEqual(len(self.registry.handlers_for(post_save, Employee)), 1)
        connection.set_tenant(self.beta)
        self.assertEqual(self.registry.handlers_for(post_save, Employee), ())
        connection.set_tenant(self.alpha)
        self.assertEqual(self.registry.config.loaded_schemas(), ["t_alpha", "t_beta"])

    def test_automation_fires_only_in_its_own_schema(self):
        from horilla.testkit import make_company, make_employee

        self._automation()
        company = make_company("Alpha Auto Co")
        with self.captureOnCommitCallbacks(execute=False) as alpha_callbacks:
            make_employee(company=company, email="auto1@alpha.test")
        connection.set_tenant(self.beta)
        beta_company = make_company("Beta Auto Co")
        with self.captureOnCommitCallbacks(execute=False) as beta_callbacks:
            make_employee(company=beta_company, email="auto1@beta.test")
        connection.set_tenant(self.alpha)

        def mail_threads(callbacks):
            # The automation handler defers
            # ``lambda: TenantAwareThread(target=lambda: send_automated_mail(...))``.
            def sends_mail(cb):
                code = getattr(cb, "__code__", None)
                return code is not None and any(
                    "send_automated_mail" in getattr(const, "co_names", ())
                    for const in code.co_consts
                )

            return [cb for cb in callbacks if sends_mail(cb)]

        # make_employee saves the Employee more than once; each save is handled.
        self.assertGreaterEqual(len(mail_threads(alpha_callbacks)), 1)
        self.assertEqual(mail_threads(beta_callbacks), [])

    def test_refresh_methods_invalidate_instead_of_global_reload(self):
        from employee.models import Employee
        from horilla_automations.signals import REFRESH_METHODS

        self.assertEqual(self.registry.handlers_for(post_save, Employee), ())
        from horilla_automations.models import MailAutomation

        # A row written without signals (e.g. by another worker) appears after
        # the views' refresh hook runs.
        MailAutomation.objects.bulk_create(
            [
                MailAutomation(
                    title="Bulk",
                    method_title="bulk",
                    model="employee.models.Employee",
                    mail_to="[]",
                    mail_details="employee_id",
                    trigger="on_create",
                    condition="",
                    condition_querystring="",
                )
            ]
        )
        self.assertEqual(self.registry.handlers_for(post_save, Employee), ())
        REFRESH_METHODS["clear_connection"]()
        REFRESH_METHODS["start_connection"]()
        self.assertEqual(len(self.registry.handlers_for(post_save, Employee)), 1)


class PmsBonusAutomationPerTenantTests(SaasTestCase):
    def test_bonus_handlers_are_loaded_per_schema(self):
        from pms import signals as pms_signals
        from pms.models import BonusPointSetting, EmployeeObjective

        pms_signals.start_automation()
        registry = pms_signals.tenant_registry()
        registry.config.clear()
        self.addCleanup(registry.config.clear)

        BonusPointSetting.objects.create(
            model="pms.models.EmployeeObjective",
            applicable_for="owner",
            bonus_for="completed",
            points=5,
        )
        self.assertEqual(len(registry.handlers_for(post_save, EmployeeObjective)), 1)
        connection.set_tenant(self.beta)
        self.assertEqual(registry.handlers_for(post_save, EmployeeObjective), ())


class AuditConfigPerTenantTests(SaasTestCase):
    def setUp(self):
        super().setUp()
        from horilla_audit import registry

        self.targets = registry.tenant_audit_targets()
        self.targets.clear()
        self.addCleanup(self.targets.clear)

    def test_tracked_models_and_fields_are_per_tenant(self):
        from auditlog.models import LogEntry
        from auditlog.registry import auditlog

        from base.models import Company
        from horilla.testkit import make_company
        from horilla_audit.models import AuditModelConfig

        AuditModelConfig.objects.create(
            app_label="base", model_name="Company", tracked_fields=["company"]
        )
        alpha_targets = self.targets.get()
        self.assertIn(Company, alpha_targets)
        self.assertTrue(auditlog.contains(Company))  # registered process-wide
        self.assertEqual(auditlog.get_model_fields(Company)["include_fields"], ["company"])

        make_company("Audited Alpha")
        self.assertTrue(
            LogEntry.objects.filter(content_type__model="company").exists(),
            "alpha tracks Company",
        )

        connection.set_tenant(self.beta)
        beta_targets = self.targets.get()
        self.assertNotIn(Company, beta_targets)  # beta runs the defaults
        make_company("Not Audited Beta")
        self.assertFalse(
            LogEntry.objects.filter(content_type__model="company").exists(),
            "beta does not track Company",
        )
        connection.set_tenant(self.alpha)

    def test_public_schema_logs_nothing(self):
        from base.models import Company
        from horilla_audit.registry import _tenant_pre_log

        connection.set_schema_to_public()
        self.assertFalse(_tenant_pre_log(sender=Company))
        connection.set_tenant(self.alpha)
        self.assertIsNone(_tenant_pre_log(sender=__import__("employee.models").models.Employee))


class LdapSaasTests(SaasTestCase):
    def test_ldap_backend_is_refused_in_saas_mode(self):
        from horilla_ldap.apps import check_ldap_backend_not_in_saas_mode

        self.assertEqual(check_ldap_backend_not_in_saas_mode(), [])
        with override_settings(
            AUTHENTICATION_BACKENDS=[
                "axes.backends.AxesStandaloneBackend",
                "django_auth_ldap.backend.LDAPBackend",
            ]
        ):
            errors = check_ldap_backend_not_in_saas_mode()
        self.assertEqual([e.id for e in errors], ["orvahr_saas.E101"])


class KeyingTests(SaasTestCase):
    def test_biometric_thread_key_includes_schema(self):
        alpha_key = tenancy.schema_key("device-1")
        connection.set_tenant(self.beta)
        self.assertNotEqual(tenancy.schema_key("device-1"), alpha_key)
        self.assertEqual(alpha_key, ("t_alpha", "device-1"))

    def test_cloud_storage_location_is_prefixed_per_tenant(self):
        from horilla.tenant_storage import TenantPrefixedLocationMixin

        class Storage(TenantPrefixedLocationMixin):
            default_location = "private"

        storage = Storage()
        self.assertEqual(storage.location, "private/t_alpha")
        connection.set_tenant(self.beta)
        self.assertEqual(storage.location, "private/t_beta")
        storage.location = "ns/"  # what django-storages' __init__ may set
        self.assertEqual(storage.location, "ns/t_beta")
        connection.set_schema_to_public()
        self.assertEqual(storage.location, "ns/")
