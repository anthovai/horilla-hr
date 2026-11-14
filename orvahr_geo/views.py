"""
Read-only JSON lookups for Thai addresses (login required).

Payloads are small (one level of the hierarchy at a time) and cached: the
data only changes when ``load_th_geo`` runs, which bumps the cache version
embedded in every key.
"""

import re

from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from horilla.decorators import login_required
from orvahr_geo.loader import cache_version
from orvahr_geo.models import District, Province, Subdistrict

CACHE_SECONDS = 60 * 60 * 24
BROWSER_CACHE = "private, max-age=3600"


def _cached(key, build):
    full_key = f"orvahr_geo:{cache_version()}:{key}"
    data = cache.get(full_key)
    if data is None:
        data = build()
        cache.set(full_key, data, CACHE_SECONDS)
    return data


def _json(items):
    response = JsonResponse({"results": items})
    response["Cache-Control"] = BROWSER_CACHE
    return response


def _bad_request(message):
    return JsonResponse({"error": message, "results": []}, status=400)


def _subdistrict_row(sub):
    return {
        "code": sub.code,
        "th": sub.name_th,
        "en": sub.name_en,
        "postcode": sub.postcode,
        "postcodes": sub.postcode_list,
    }


@login_required
@require_GET
def provinces(request):
    return _json(
        _cached(
            "provinces",
            lambda: [
                {"code": p.code, "iso": p.iso_code, "th": p.name_th, "en": p.name_en}
                for p in Province.objects.order_by("name_th")
            ],
        )
    )


@login_required
@require_GET
def districts(request):
    code = request.GET.get("province", "")
    if not re.fullmatch(r"\d{2}", code):
        return _bad_request("province must be a 2-digit province code")
    return _json(
        _cached(
            f"districts:{code}",
            lambda: [
                {"code": d.code, "th": d.name_th, "en": d.name_en}
                for d in District.objects.filter(province__code=code).order_by(
                    "name_th"
                )
            ],
        )
    )


@login_required
@require_GET
def subdistricts(request):
    code = request.GET.get("district", "")
    if not re.fullmatch(r"\d{4}", code):
        return _bad_request("district must be a 4-digit district code")
    return _json(
        _cached(
            f"subdistricts:{code}",
            lambda: [
                _subdistrict_row(s)
                for s in Subdistrict.objects.filter(district__code=code).order_by(
                    "name_th"
                )
            ],
        )
    )


def subdistricts_for_postcode(postcode):
    """Subdistricts served by a postcode (primary or secondary)."""
    # postcodes is a space separated list of 5-digit codes, so a substring
    # match on a 5-digit code can only hit a whole entry.
    return (
        Subdistrict.objects.filter(postcodes__contains=postcode)
        .select_related("district__province")
        .order_by("code")
    )


@login_required
@require_GET
def postcode_search(request):
    postcode = request.GET.get("postcode", "")
    if not re.fullmatch(r"\d{5}", postcode):
        return _bad_request("postcode must be 5 digits")

    def build():
        rows = []
        for sub in subdistricts_for_postcode(postcode):
            district = sub.district
            province = district.province
            row = _subdistrict_row(sub)
            row["district"] = {
                "code": district.code,
                "th": district.name_th,
                "en": district.name_en,
            }
            row["province"] = {
                "code": province.code,
                "iso": province.iso_code,
                "th": province.name_th,
                "en": province.name_en,
            }
            rows.append(row)
        return rows

    return _json(_cached(f"postcode:{postcode}", build))
