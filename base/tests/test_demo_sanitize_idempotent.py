"""apply_replacements must be idempotent: its output is written back to the
fixture files on every demo load (scrub_side_fixture_files)."""

from django.test import SimpleTestCase

from base.demo_data.catalog import STRING_REPLACEMENTS
from base.demo_data.sanitize import apply_replacements


class ApplyReplacementsIdempotentTests(SimpleTestCase):
    def test_running_twice_equals_running_once(self):
        for old, _new in STRING_REPLACEMENTS:
            once = apply_replacements(f"x {old} y")
            self.assertEqual(apply_replacements(once), once, old)

    def test_already_replaced_text_does_not_grow(self):
        self.assertEqual(
            apply_replacements("System Administrator"), "System Administrator"
        )
        self.assertEqual(apply_replacements("System Admin"), "System Administrator")
        self.assertEqual(apply_replacements("Sales Manager"), "Sales Manager")
        self.assertEqual(apply_replacements("Sales Man"), "Sales Associate")

    def test_a_short_pair_does_not_eat_a_longer_one(self):
        self.assertEqual(
            apply_replacements("Odoo Developer Template"), "Software Engineer Assessment"
        )
