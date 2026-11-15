"""
orvahr_account tests.

``test_subscription_pages`` needs SaaS mode (PostgreSQL schemas, orvahr_billing
installed); outside it only ``test_flag_off`` runs. Choosing the modules here,
rather than skipping at import time, keeps ``manage.py test --parallel`` working:
a module skipped on import cannot be sent to a parallel worker.
"""

import os


def load_tests(loader, standard_tests, pattern):
    from django.conf import settings

    here = os.path.dirname(__file__)
    if not getattr(settings, "ORVAHR_MULTI_TENANT", False):
        standard_tests.addTests(loader.discover(start_dir=here, pattern="test_flag_off.py"))
        return standard_tests
    standard_tests.addTests(loader.discover(start_dir=here, pattern=pattern or "test*.py"))
    return standard_tests
