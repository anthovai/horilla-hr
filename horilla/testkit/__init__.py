"""
Shared Django test helpers for OrvaHR unit tests.

Prefer these factories over copying setUp boilerplate across apps.

The helpers are imported lazily (PEP 562): ``horilla.testkit.saas_runner`` is
imported by ``--parallel`` workers *before* ``django.setup()``, and importing
the factories (models) at package import would fail there.
"""

import importlib

_EXPORTS = {
    "CompanyFilterTestMixin": "horilla.testkit.company",
    "clear_selected_company": "horilla.testkit.company",
    "make_company": "horilla.testkit.factories",
    "make_employee": "horilla.testkit.factories",
    "make_user": "horilla.testkit.factories",
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value
