"""
SaaS-mode billing tests (PostgreSQL, ORVAHR_MULTI_TENANT=1):

    python manage.py test orvahr_billing --keepdb

Outside SaaS mode orvahr_billing is not installed and nothing is collected.
"""

import os


def load_tests(loader, standard_tests, pattern):
    from django.conf import settings

    if not getattr(settings, "ORVAHR_MULTI_TENANT", False):
        return standard_tests
    standard_tests.addTests(
        loader.discover(
            start_dir=os.path.dirname(__file__), pattern=pattern or "test*.py"
        )
    )
    return standard_tests
