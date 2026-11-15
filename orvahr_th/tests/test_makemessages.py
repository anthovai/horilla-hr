"""The project's makemessages must not fuzzy-match or add source locations."""

from django.core.management import get_commands, load_command_class
from django.test import SimpleTestCase


class MakemessagesOverrideTests(SimpleTestCase):
    def test_project_command_replaces_django_builtin(self):
        self.assertEqual(get_commands()["makemessages"], "orvahr_th")

    def test_msgmerge_runs_without_fuzzy_matching(self):
        command = load_command_class("orvahr_th", "makemessages")
        self.assertIn("--no-fuzzy-matching", command.msgmerge_options)
        # Django's own options are kept, not replaced.
        self.assertIn("--previous", command.msgmerge_options)
