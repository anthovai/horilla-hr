"""
makemessages without fuzzy matching and without source locations.

Stock makemessages runs msgmerge with fuzzy matching on. When a msgid is new
but resembles an existing one, msgmerge copies the old translation and marks
it ``#, fuzzy``. compilemessages skips fuzzy entries, so after a refresh those
strings silently fall back to English, and the ones that do get reviewed often
carry a translation of a *different* sentence. On the Thai catalog a single
refresh produced 417 such entries.

The catalogs are also kept without ``#:`` location comments; stock
makemessages would add ~32,000 of them and turn every refresh into a
whole-file diff.

New strings arrive as plain untranslated entries instead, which
``msgfmt --statistics`` counts and a translator can find.
"""

from django.core.management.commands import makemessages


class Command(makemessages.Command):
    msgmerge_options = makemessages.Command.msgmerge_options + [
        "--no-fuzzy-matching"
    ]

    def handle(self, *args, **options):
        options["no_location"] = True
        return super().handle(*args, **options)
