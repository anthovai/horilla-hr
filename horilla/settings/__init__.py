"""
horilla/settings/__init__.py
Combines official base settings + client overrides.
"""

from .base import *

# Import client overrides (if file exists)
try:
    from .addons import *
    from .local_settings import *
except ImportError:
    pass

# Multi-tenant (SaaS) mode: one PostgreSQL schema per customer. Off unless
# ORVAHR_MULTI_TENANT is set; applied last so it sees every override above.
if ORVAHR_MULTI_TENANT:
    from .saas import apply_multi_tenant

    apply_multi_tenant(globals())
