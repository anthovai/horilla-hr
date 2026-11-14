"""Viewing unmasked Thai identifiers is recorded in the PDPA access log."""

from orvahr_pdpa.models import SensitiveAccessLog

from employee.tests.test_thai_info_tab import NID, ThaiInfoTabBase


class ThaiInfoAccessLogTests(ThaiInfoTabBase):
    def test_hr_viewing_full_ids_is_logged_with_field_names_only(self):
        response = self.get_tab(self.hr_user)
        self.assertEqual(response.status_code, 200)
        log = SensitiveAccessLog.all_objects.get(subject_employee=self.target)
        self.assertEqual(log.context, "thai_info_tab")
        self.assertIn("national_id", log.fields)
        self.assertNotIn(NID, str(log.fields) + log.subject_label)

    def test_masked_views_are_not_logged(self):
        self.get_tab(self.self_user)
        self.assertFalse(SensitiveAccessLog.all_objects.exists())
