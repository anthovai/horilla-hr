"""
Thai in PDFs: the embedded Sarabun font, the pre-shaping for reportlab and
the ToUnicode patch that keeps the text extractable.

How it looks (checked by rendering to PNG and looking, 2026-11-14): with
Sarabun PDF + shape(), tone marks over upper vowels (ที่, สี่), marks on
ป ฝ ฟ and tone + sara am (น้ำ) are placed correctly. Without it, reportlab
drew the tone inside the upper vowel, so "สี่" printed as "สี".
Not handled: kerning, and Thai line breaking (xhtml2pdf only breaks at
spaces, so Thai runs need wide enough cells).
"""

import io
import os
import re

from django.template import Context, Template
from django.test import SimpleTestCase
from django.utils import translation

from orvahr_th import pdf, thai_shaping

try:
    import pypdf
except ImportError:  # not a runtime dependency
    pypdf = None

SAMPLE = "หนังสือรับรองการหักภาษี ณ ที่จ่าย สามสิบสี่บาท ปี่ ญี่ปุ่น น้ำ ฟ้า ปู่ ฐุ ญุ"


def font_names(data):
    return set(re.findall(rb"/BaseFont\s*/([A-Z]{6}\+[A-Za-z-]+)", data))


class ShapeTests(SimpleTestCase):
    def pua(self, kind, ch):
        return chr(thai_shaping.CODEPOINT[(kind, ch)])

    def test_tone_over_upper_vowel_is_raised(self):
        shaped = thai_shaping.shape("ที่")
        self.assertEqual(shaped, "ที" + self.pua("high", "่"))

    def test_marks_on_ascenders_move_left(self):
        self.assertEqual(thai_shaping.shape("ป่า"), "ป" + self.pua("left", "่") + "า")
        self.assertEqual(
            thai_shaping.shape("ปี่"),
            "ป" + self.pua("left", "ี") + self.pua("high_left", "่"),
        )

    def test_tone_before_sara_am_is_raised(self):
        self.assertEqual(thai_shaping.shape("น้ำ"), "น" + self.pua("high", "้") + "ำ")

    def test_tailless_before_lower_vowel(self):
        self.assertEqual(thai_shaping.shape("ญุ"), self.pua("tailless", "ญ") + "ุ")
        self.assertEqual(thai_shaping.shape("ญา"), "ญา")

    def test_plain_text_untouched(self):
        for text in ("", "Payslip 1,234.50", "กา", "ก่", "<p class='x'>ok</p>", None, 5):
            self.assertEqual(thai_shaping.shape(text), text)

    def test_every_variant_maps_back(self):
        self.assertEqual(len(thai_shaping.ORIGINAL), len(thai_shaping.VARIANTS))
        for code, original in thai_shaping.ORIGINAL.items():
            self.assertTrue(0xF700 <= code <= 0xF8FF)
            self.assertTrue(0x0E00 <= original <= 0x0E7F)


class FontTests(SimpleTestCase):
    def test_font_files_and_licence_ship_with_the_app(self):
        for shaped in (False, True):
            for weight in ("normal", "bold"):
                path = pdf.font_path(weight, shaped=shaped)
                self.assertTrue(os.path.isfile(path), path)
                with open(path, "rb") as fh:
                    self.assertEqual(fh.read(4), b"\x00\x01\x00\x00")  # TrueType
        licence = os.path.join(os.path.dirname(pdf.font_path()), "OFL.txt")
        self.assertTrue(os.path.isfile(licence))

    def test_pdf_font_has_the_variant_glyphs(self):
        from reportlab.pdfbase.ttfonts import TTFontFile

        face = TTFontFile(pdf.font_path("normal", shaped=True))
        for code in thai_shaping.ORIGINAL:
            self.assertIn(code, face.charToGlyph, hex(code))

    def test_font_face_css_per_engine(self):
        web = pdf.font_face_css(pdf.ENGINE_WEB)
        self.assertIn("/orvahr_th/fonts/Sarabun-Regular.ttf", web)
        self.assertIn("font-weight: bold", web)
        wk = pdf.font_face_css(pdf.ENGINE_WKHTMLTOPDF)
        self.assertIn("file:///", wk)
        self.assertIn("Sarabun-Regular.ttf", wk)
        x2p = pdf.font_face_css(pdf.ENGINE_XHTML2PDF)
        self.assertIn("SarabunPDF-Regular.ttf", x2p)
        self.assertNotIn("file://", x2p)


class Xhtml2pdfThaiTests(SimpleTestCase):
    def render(self, body):
        html = (
            "<html><head><meta charset='utf-8'><style>%s "
            "body { font-family: 'Sarabun'; font-size: 12pt; }</style></head>"
            "<body>%s</body></html>" % (pdf.font_face_css(pdf.ENGINE_XHTML2PDF), body)
        )
        return pdf.html_to_pdf_xhtml2pdf(html)

    def test_sarabun_is_embedded(self):
        data = self.render(f"<p>{SAMPLE}</p><p><b>{SAMPLE}</b></p>")
        self.assertTrue(data.startswith(b"%PDF"))
        names = {n.split(b"+", 1)[1] for n in font_names(data)}
        self.assertIn(b"SarabunPDF-Regular", names)
        self.assertIn(b"SarabunPDF-Bold", names)
        self.assertIn(b"/FontFile2", data)  # the TrueType program is embedded

    def test_tounicode_maps_variants_back_to_thai(self):
        from reportlab.pdfbase import ttfonts

        thai_shaping.install_tounicode_patch()
        pua = thai_shaping.CODEPOINT[("high", "\u0e48")]
        cmap = ttfonts.makeToUnicodeCMap("X", [0, 0x0E01, pua, 0x41])
        self.assertIn("<02> <0E48>", cmap)
        self.assertIn("<01> <0E01>", cmap)
        self.assertIn("<03> <0041>", cmap)
        self.assertNotIn("%04X" % pua, cmap)

    def test_text_extracts_as_standard_thai(self):
        if pypdf is None:
            self.skipTest("pypdf not installed")
        data = self.render(f"<p>{SAMPLE}</p>")
        text = pypdf.PdfReader(io.BytesIO(data)).pages[0].extract_text()
        self.assertEqual(text.strip(), SAMPLE)
        self.assertFalse(any(0xF700 <= ord(c) <= 0xF8FF for c in text))

    def test_remote_resources_are_not_fetched(self):
        callback = pdf.make_link_callback("testserver")
        self.assertEqual(callback("https://fonts.googleapis.com/css?family=Inter", None), "")
        self.assertEqual(callback("http://evil.example/x.png", None), "")
        self.assertEqual(callback("file:///etc/passwd", None), "")
        self.assertEqual(callback("/etc/passwd", None), "")
        font = pdf.font_path("normal", shaped=True)
        self.assertEqual(callback(font, None), font)


class TemplateTagTests(SimpleTestCase):
    def render(self, source, **context):
        return Template("{% load thai %}" + source).render(Context(context))

    def test_filters(self):
        import datetime
        from decimal import Decimal

        d = datetime.date(2026, 11, 14)
        self.assertEqual(self.render("{{ d|be_date }}", d=d), "14 พฤศจิกายน 2569")
        self.assertEqual(self.render('{{ d|be_date:"short" }}', d=d), "14 พ.ย. 69")
        self.assertEqual(self.render("{{ d|be_year }}", d=d), "2569")
        self.assertEqual(
            self.render("{{ v|baht_text }}", v=Decimal("1234.50")),
            "หนึ่งพันสองร้อยสามสิบสี่บาทห้าสิบสตางค์",
        )
        self.assertEqual(self.render("{{ v|baht_text }}", v="junk"), "")
        self.assertEqual(self.render("{{ v|thai_id }}", v="1234567890121"), "1-2345-67890-12-1")
        self.assertEqual(
            self.render("{{ v|mask_thai_id }}", v="1234567890121"), "X-XXXX-XXXX0-12-1"
        )
        self.assertEqual(self.render("{{ c|is_baht }}", c="฿"), "True")
        self.assertEqual(self.render("{{ c|is_baht }}", c="$"), "False")

    def test_local_date_follows_the_language(self):
        import datetime

        d = datetime.date(2026, 11, 14)
        with translation.override("th"):
            self.assertEqual(self.render("{{ d|local_date }}", d=d), "14 พฤศจิกายน 2569")
        with translation.override("en"):
            self.assertEqual(self.render("{{ d|local_date }}", d=d), "November 14, 2026")
