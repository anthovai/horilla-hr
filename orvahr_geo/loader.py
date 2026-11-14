"""
Load the shipped Thai geography fixture into Province/District/Subdistrict.

Idempotent: rows are matched by their DOPA code, missing rows are created,
changed rows updated, unchanged rows left alone. Rows no longer in the
fixture are reported but never deleted.

Model classes are parameters so the 0002 data migration can pass its
historical models.
"""

import json
from pathlib import Path

from django.core.cache import cache
from django.db import transaction

FIXTURE_PATH = Path(__file__).resolve().parent / "data" / "th_geo.json"
CACHE_VERSION_KEY = "orvahr_geo:data_version"


def read_fixture(path=None):
    with open(path or FIXTURE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def iso_code(province_code):
    """ISO 3166-2:TH code of a province: the same number as the DOPA code."""
    return f"TH-{province_code}"


def _sync(model, rows, fields):
    """rows: {code: {field: value}}. Returns (created, updated, stale)."""
    existing = {obj.code: obj for obj in model.objects.all()}
    to_create, to_update = [], []
    for code, values in rows.items():
        obj = existing.get(code)
        if obj is None:
            to_create.append(model(code=code, **values))
            continue
        changed = False
        for field, value in values.items():
            if getattr(obj, field) != value:
                setattr(obj, field, value)
                changed = True
        if changed:
            to_update.append(obj)
    model.objects.bulk_create(to_create, batch_size=500)
    if to_update:
        model.objects.bulk_update(to_update, fields, batch_size=500)
    stale = len(set(existing) - set(rows))
    return len(to_create), len(to_update), stale


def load_th_geo(Province, District, Subdistrict, path=None, bump_cache=True):
    data = read_fixture(path)
    result = {}
    with transaction.atomic():
        result["provinces"] = _sync(
            Province,
            {
                code: {"name_th": th, "name_en": en, "iso_code": iso_code(code)}
                for code, th, en in data["provinces"]
            },
            ["name_th", "name_en", "iso_code"],
        )
        province_ids = dict(Province.objects.values_list("code", "id"))
        result["districts"] = _sync(
            District,
            {
                code: {
                    "name_th": th,
                    "name_en": en,
                    "province_id": province_ids[code[:2]],
                }
                for code, th, en in data["districts"]
            },
            ["name_th", "name_en", "province_id"],
        )
        district_ids = dict(District.objects.values_list("code", "id"))
        result["subdistricts"] = _sync(
            Subdistrict,
            {
                code: {
                    "name_th": th,
                    "name_en": en,
                    "district_id": district_ids[code[:4]],
                    "postcode": postcodes.split()[0],
                    "postcodes": postcodes,
                }
                for code, th, en, postcodes in data["subdistricts"]
            },
            ["name_th", "name_en", "district_id", "postcode", "postcodes"],
        )
    changed = any(created or updated for created, updated, _stale in result.values())
    if bump_cache and changed:
        bump_cache_version()
    return result


def bump_cache_version():
    """Invalidate every cached endpoint payload (keys embed the version)."""
    try:
        cache.incr(CACHE_VERSION_KEY)
    except ValueError:
        cache.set(CACHE_VERSION_KEY, 2, None)


def cache_version():
    version = cache.get(CACHE_VERSION_KEY)
    if version is None:
        cache.add(CACHE_VERSION_KEY, 1, None)
        version = cache.get(CACHE_VERSION_KEY, 1)
    return version
