"""
Thai template filters: ``{% load thai %}``.

    {{ payslip.end_date|be_date }}            14 พฤศจิกายน 2569
    {{ payslip.end_date|be_date:"short" }}    14 พ.ย. 69
    {{ payslip.end_date|be_year }}            2569
    {{ payslip.end_date|local_date }}         BE when the active language is Thai,
                                              "November 14, 2026" otherwise
    {{ net_pay|baht_text }}                   หนึ่งพันบาทถ้วน
    {{ profile.tax_id|thai_id }}              1-2345-67890-12-1
    {{ profile.tax_id|mask_thai_id }}         X-XXXX-XXXX0-12-1
    {% thai_font_face pdf_engine %}           @font-face rules for Sarabun

Filters never raise on bad input: they return "" (or the value unchanged for
``thai_id``) so a template keeps rendering.
"""

from django import template
from django.template.defaultfilters import date as date_filter
from django.utils import translation

from orvahr_th import formatting
from orvahr_th.pdf import ENGINE_WEB, font_face_css

register = template.Library()


def is_thai_language(language=None):
    language = language or translation.get_language() or ""
    return language.lower().startswith("th")


@register.filter
def be_date(value, style="long"):
    return formatting.be_date(value, style or "long")


@register.filter
def be_year(value):
    year = formatting.be_year(value)
    return "" if year is None else year


@register.filter
def local_date(value, style="long"):
    """BE Thai date under a Thai locale; ``F j, Y`` (localised) otherwise."""
    date = formatting.to_date(value)
    if date is None:
        return ""
    if is_thai_language():
        return formatting.be_date(date, style or "long")
    return date_filter(date, "M j, Y" if style == "short" else "F j, Y")


@register.filter
def baht_text(value):
    try:
        return formatting.baht_text(value)
    except (ValueError, TypeError, ArithmeticError):
        return ""


@register.filter
def thai_id(value):
    return formatting.format_thai_id(value)


@register.filter
def mask_thai_id(value):
    return formatting.mask_thai_id(value)


@register.filter
def is_baht(currency):
    """True for a currency symbol / code that means Thai baht."""
    return str(currency or "").strip().upper() in ("฿", "THB", "บาท")


@register.simple_tag
def thai_font_face(engine=None):
    return font_face_css(engine or ENGINE_WEB)


@register.simple_tag
def thai_language():
    return is_thai_language()
