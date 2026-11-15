"""
PDF of a billing document, Thai (bilingual Thai/English labels -- the legal
copy) or English, rendered by orvahr_th.pdf (xhtml2pdf + Sarabun).
"""

import base64
import mimetypes

from django.template.loader import render_to_string
from django.utils import translation

from orvahr_billing.models import HEAD_OFFICE, DocType, SellerSettings
from orvahr_th.formatting import format_thai_id
from orvahr_th.pdf import ENGINE_XHTML2PDF, html_to_pdf_xhtml2pdf

LANGUAGES = ("th", "en")


def branch_label(code, lang):
    if not code:
        return ""
    if code == HEAD_OFFICE:
        return "สำนักงานใหญ่" if lang == "th" else "Head office"
    return f"สาขาที่ {code}" if lang == "th" else f"Branch {code}"


def _logo_data_uri():
    """The seller logo inlined (it lives in private storage, never on a URL)."""
    seller = SellerSettings.objects.filter(pk=1).first()
    if not seller or not seller.logo:
        return ""
    try:
        with seller.logo.open("rb") as handle:
            data = handle.read()
    except OSError:
        return ""
    mime = mimetypes.guess_type(seller.logo.name)[0] or "image/png"
    if mime not in ("image/png", "image/jpeg", "image/gif"):
        return ""
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def document_context(doc, lang="th"):
    lang = lang if lang in LANGUAGES else "th"
    related = doc.related_document
    return {
        "doc": doc,
        "lang": lang,
        "th": lang == "th",
        "lines": list(doc.lines.all()),
        "is_invoice": doc.doc_type == DocType.INVOICE,
        "is_receipt": doc.doc_type == DocType.RECEIPT,
        "is_credit_note": doc.doc_type == DocType.CREDIT_NOTE,
        "related": related,
        "seller_tax_id": format_thai_id(doc.seller_tax_id),
        "seller_branch": branch_label(doc.seller_branch_code, lang),
        "buyer_tax_id": format_thai_id(doc.buyer_tax_id) if doc.buyer_tax_id else "",
        "buyer_branch": branch_label(doc.buyer_branch_code, lang),
        "logo": _logo_data_uri(),
        "pdf_engine": ENGINE_XHTML2PDF,
    }


def render_html(doc, lang="th"):
    # English labels come from gettext msgids: render them untranslated so
    # the Thai copy reads "ไทย / English" and the English copy reads English.
    with translation.override("en"):
        return render_to_string(
            "orvahr_billing/document_pdf.html", document_context(doc, lang)
        )


def render_pdf(doc, lang="th"):
    return html_to_pdf_xhtml2pdf(render_html(doc, lang))
