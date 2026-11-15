"""
SaaS-mode test suite. Runs only with ORVAHR_MULTI_TENANT=1 on PostgreSQL:

    ORVAHR_MULTI_TENANT=1 \\
    DATABASE_URL=postgres://USER:PASS@HOST:PORT/DBNAME \\
    python manage.py test orvahr_saas --keepdb

In single-tenant mode every module here is skipped (orvahr_saas is not even
installed then). See orvahr_saas/README.md for a throwaway local cluster.
"""

import os


def load_tests(loader, standard_tests, pattern):
    """
    Discover the modules only in SaaS mode. Outside it orvahr_saas is not an
    installed app, so importing its models would fail; returning the empty
    suite keeps ``manage.py test`` (including ``--parallel``) clean.
    """
    from django.conf import settings

    if not getattr(settings, "ORVAHR_MULTI_TENANT", False):
        return standard_tests
    standard_tests.addTests(
        loader.discover(
            start_dir=os.path.dirname(__file__), pattern=pattern or "test*.py"
        )
    )
    return standard_tests
