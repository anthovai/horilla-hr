"""SaaS mode: one tenant's face templates are never used in another tenant."""

from unittest import mock

from django_tenants.utils import schema_context

from horilla.testkit import make_company, make_employee, make_user
from orvahr_face import consent, services
from orvahr_face.crypto import decrypt_template
from orvahr_face.models import FaceSettings, FaceTemplate
from orvahr_face.tests.helpers import FAKE_JPEG, embed_ok
from orvahr_saas.tests.base import SaasTestCase


class FaceTenantIsolationTests(SaasTestCase):
    saas_test_tenant = False

    def _setup_tenant_employee(self, name):
        company = make_company(f"{name} Co")
        FaceSettings.all_objects.create(company=company, enabled=True)
        user = make_user(f"{name.lower()}-face", password="pass")
        employee = make_employee(company=company, email=f"{name.lower()}-face@x.test", first_name=name, user=user)
        self.assertTrue(consent.grant(employee))
        return employee

    def test_alpha_templates_never_reach_beta(self):
        service = mock.MagicMock()
        service.embed.return_value = embed_ok()
        with mock.patch("orvahr_face.services.face_client.get_client", return_value=service):
            with schema_context(self.alpha.schema_name):
                alpha_emp = self._setup_tenant_employee("Alpha")
                self.assertTrue(services.enrol(alpha_emp, [FAKE_JPEG] * 5).ok)
                token = FaceTemplate.all_objects.get(employee=alpha_emp).template_encrypted
                self.assertIsNotNone(decrypt_template(token, alpha_emp.pk))

            with schema_context(self.beta.schema_name):
                self.assertFalse(FaceTemplate.all_objects.exists())
                beta_emp = self._setup_tenant_employee("Beta")
                self.assertFalse(services.is_enrolled(beta_emp))
                outcome = services.verify(beta_emp, FAKE_JPEG)
                self.assertEqual(outcome.code, "not_enrolled")
                service.verify.assert_not_called()
                # A ciphertext copied across schemas is refused, even for the same employee id.
                self.assertIsNone(decrypt_template(token, alpha_emp.pk))
                FaceTemplate.all_objects.create(
                    employee=beta_emp, template_encrypted=token, model_id="arcface-r50", model_version="1.0"
                )
                _model, vectors = services.usable_templates(beta_emp)
                self.assertEqual(vectors, [])
