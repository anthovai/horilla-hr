"""
orvahr_face tests.

``test_saas_isolation`` needs SaaS mode (PostgreSQL schemas, two tenants) and
is only collected when ORVAHR_MULTI_TENANT is on; choosing modules here,
rather than skipping at import time, keeps ``--parallel`` working.
"""

import os


def load_tests(loader, standard_tests, pattern):
    from django.conf import settings

    here = os.path.dirname(__file__)
    saas = getattr(settings, "ORVAHR_MULTI_TENANT", False)
    for name in sorted(os.listdir(here)):
        if not (name.startswith("test") and name.endswith(".py")):
            continue
        if name == "test_saas_isolation.py" and not saas:
            continue
        standard_tests.addTests(loader.discover(start_dir=here, pattern=name, top_level_dir=None))
    return standard_tests
