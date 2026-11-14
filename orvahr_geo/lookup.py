"""Match typed address names (Thai or English) to the geography tables."""

import re

from orvahr_geo.models import Subdistrict

# Administrative prefixes people type in front of names, longest first.
_PREFIXES = (
    "จังหวัด",
    "อำเภอ",
    "ตำบล",
    "แขวง",
    "เขต",
    "จ.",
    "อ.",
    "ต.",
    "changwat ",
    "amphoe ",
    "khet ",
    "tambon ",
    "khwaeng ",
)
_SUFFIXES = (" province", " district", " subdistrict")
_ALIASES = {
    "กรุงเทพฯ": "กรุงเทพมหานคร",
    "กทม": "กรุงเทพมหานคร",
    "กทม.": "กรุงเทพมหานคร",
}


def normalize_name(value):
    value = re.sub(r"\s+", " ", (value or "").strip()).lower()
    for prefix in _PREFIXES:
        if value.startswith(prefix):
            value = value[len(prefix) :].strip()
            break
    for suffix in _SUFFIXES:
        if value.endswith(suffix):
            value = value[: -len(suffix)].strip()
            break
    return _ALIASES.get(value, value)


def _matches(obj, name):
    return name in (obj.name_th.lower(), obj.name_en.lower())


def find_subdistrict(province, district, subdistrict):
    """
    The Subdistrict whose subdistrict, district and province names all match
    (Thai or English, case-insensitive, admin prefixes ignored), or None.
    """
    names = [normalize_name(v) for v in (subdistrict, district, province)]
    if not all(names):
        return None
    sub_name, district_name, province_name = names
    candidates = Subdistrict.objects.select_related("district__province").filter(
        name_th__iexact=sub_name
    ) | Subdistrict.objects.select_related("district__province").filter(
        name_en__iexact=sub_name
    )
    for sub in candidates:
        if _matches(sub.district, district_name) and _matches(
            sub.district.province, province_name
        ):
            return sub
    return None


def get_subdistrict(code):
    if not code or not re.fullmatch(r"\d{6}", code):
        return None
    return (
        Subdistrict.objects.select_related("district__province")
        .filter(code=code)
        .first()
    )
