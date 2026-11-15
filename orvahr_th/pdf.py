"""
Thai-capable PDF rendering.

Two HTML-to-PDF engines exist in this codebase:

* wkhtmltopdf (via pdfkit) -- what payroll's payslip PDF has always used. Its
  WebKit does proper Thai shaping (tone marks stacked over upper vowels), but
  the binary is optional and is NOT installed in the Docker image.
* xhtml2pdf (reportlab) -- pure Python, always available. reportlab 4.0 has
  no OpenType shaping, so Thai combining marks are placed by their default
  glyph offsets only; see the README notes in orvahr_th/tests/test_pdf.py.

Neither engine can draw Thai without a Thai font, so Sarabun (SIL OFL 1.1,
orvahr_th/static/orvahr_th/fonts/) is embedded via @font-face.
``font_face_css(engine)`` writes the rule with a source each engine can load:
a file path for xhtml2pdf, a file:// URL for wkhtmltopdf, the static URL for
the browser.
"""

import io
import os
from urllib.parse import urlparse

from django.conf import settings
from horilla.tenancy import tenant_media_root
from django.contrib.staticfiles import finders
from django.templatetags.static import static
from django.utils.safestring import mark_safe

ENGINE_WEB = "web"
ENGINE_WKHTMLTOPDF = "wkhtmltopdf"
ENGINE_XHTML2PDF = "xhtml2pdf"

FONT_FAMILY = "Sarabun"
FONT_FILES = {
    "normal": "orvahr_th/fonts/Sarabun-Regular.ttf",
    "bold": "orvahr_th/fonts/Sarabun-Bold.ttf",
}
# Sarabun plus pre-positioned Thai mark glyphs, for reportlab, which cannot
# shape. See orvahr_th/thai_shaping.py and orvahr_th/tools/build_pdf_fonts.py.
PDF_FONT_FILES = {
    "normal": "orvahr_th/fonts/SarabunPDF-Regular.ttf",
    "bold": "orvahr_th/fonts/SarabunPDF-Bold.ttf",
}

_APP_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def font_path(weight="normal", shaped=False):
    """
    Absolute filesystem path of the Sarabun TTF for ``weight``; with
    ``shaped`` the Sarabun PDF variant that xhtml2pdf needs.
    """
    relative = (PDF_FONT_FILES if shaped else FONT_FILES)[weight]
    found = finders.find(relative)
    if isinstance(found, (list, tuple)):
        found = found[0] if found else None
    return os.path.realpath(found or os.path.join(_APP_STATIC, relative))


def _font_src(weight, engine):
    if engine == ENGINE_XHTML2PDF:
        return font_path(weight, shaped=True)
    if engine == ENGINE_WKHTMLTOPDF:
        return "file://" + font_path(weight)
    return static(FONT_FILES[weight])


def font_face_css(engine=ENGINE_WEB):
    """@font-face rules for Sarabun regular and bold, for ``engine``."""
    rules = []
    for weight in ("normal", "bold"):
        rules.append(
            "@font-face { font-family: '%s'; src: url('%s'); font-weight: %s; "
            "font-style: normal; }" % (FONT_FAMILY, _font_src(weight, engine), weight)
        )
    return mark_safe("\n".join(rules))


def wkhtmltopdf_available():
    """True when pdfkit can find a wkhtmltopdf binary."""
    try:
        from base.methods import pdfkit_configuration

        return pdfkit_configuration() is not None
    except Exception:  # pdfkit missing or misconfigured
        return False


def _local_path_for(uri, request_host=None):
    """
    Map a URI in the document to a local file, or None.

    Only files under the static finders, STATIC_ROOT, MEDIA_ROOT or this app's
    font directory are ever returned: xhtml2pdf would otherwise fetch remote
    URLs (Google Fonts, an absolute logo URL) at render time, which is slow and
    lets document content make the server issue requests.
    """
    if not uri:
        return None
    parsed = urlparse(uri)
    path = uri
    if parsed.scheme in ("http", "https"):
        if not request_host or parsed.netloc != request_host:
            return None
        path = parsed.path
    elif parsed.scheme == "file":
        path = parsed.path
    elif parsed.scheme:
        return None

    font_dir = os.path.realpath(os.path.join(_APP_STATIC, "orvahr_th", "fonts"))
    if os.path.isabs(path) and os.path.realpath(path).startswith(font_dir + os.sep):
        return os.path.realpath(path) if os.path.isfile(path) else None

    static_url = settings.STATIC_URL or "/static/"
    media_url = settings.MEDIA_URL or "/media/"
    candidates = []
    if path.startswith(static_url):
        relative = path[len(static_url) :]
        found = finders.find(relative)
        if found:
            candidates.append(found if isinstance(found, str) else found[0])
        if settings.STATIC_ROOT:
            candidates.append(os.path.join(settings.STATIC_ROOT, relative))
        root = settings.STATIC_ROOT
    elif path.startswith(media_url) and settings.MEDIA_ROOT:
        relative = path[len(media_url) :]
        # SaaS mode: the active tenant's folder, never another tenant's.
        media_root = tenant_media_root()
        candidates.append(os.path.join(media_root, relative))
        root = media_root
    else:
        return None
    for candidate in candidates:
        real = os.path.realpath(candidate)
        allowed = [os.path.realpath(r) for r in (root,) if r] + [
            os.path.realpath(d) for d in _static_dirs()
        ]
        if os.path.isfile(real) and any(
            real.startswith(base + os.sep) for base in allowed
        ):
            return real
    return None


def _static_dirs():
    dirs = []
    for finder in finders.get_finders():
        for storage in getattr(finder, "storages", {}).values():
            location = getattr(storage, "location", None)
            if location:
                dirs.append(location)
    for entry in getattr(settings, "STATICFILES_DIRS", []):
        dirs.append(entry[1] if isinstance(entry, (list, tuple)) else entry)
    return dirs


def make_link_callback(request_host=None):
    """xhtml2pdf link_callback that only resolves local static/media/fonts."""

    def link_callback(uri, rel):
        local = _local_path_for(uri, request_host)
        # An empty string makes xhtml2pdf skip the resource instead of
        # fetching it.
        return local or ""

    return link_callback


def html_to_pdf_xhtml2pdf(html, request_host=None):
    """
    Render ``html`` with xhtml2pdf; returns PDF bytes. Raises on failure.

    Thai text is rewritten for Sarabun PDF first (see thai_shaping), so the
    document's Thai must be set in the font from ``font_face_css(ENGINE_XHTML2PDF)``.
    """
    from xhtml2pdf import pisa

    from orvahr_th.thai_shaping import install_tounicode_patch, shape

    install_tounicode_patch()
    buffer = io.BytesIO()
    status = pisa.CreatePDF(
        src=shape(html),
        dest=buffer,
        encoding="utf-8",
        link_callback=make_link_callback(request_host),
    )
    if status.err:
        raise RuntimeError(f"xhtml2pdf reported {status.err} error(s)")
    return buffer.getvalue()
