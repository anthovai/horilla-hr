"""
Poor man's Thai shaping for reportlab / xhtml2pdf.

reportlab 4.0 draws a TrueType font glyph by glyph, with no OpenType GSUB/GPOS.
Thai needs GPOS: Sarabun's tone marks sit, by default, at the height of an
upper vowel, so in "ที่" the mai ek is drawn *inside* the sara ii and vanishes
-- "สี่" (four) prints as "สี" (colour), which on a tax document changes an
amount written in words. Marks over the tall consonants ป ฝ ฟ also collide
with the ascender, and ญ / ฐ keep their tail under a lower vowel.

The fix is the one Thai fonts used before OpenType: pre-positioned variant
glyphs in the Private Use Area. ``orvahr_th/tools/build_pdf_fonts.py`` derives
"Sarabun PDF" from Sarabun, adding the glyphs in ``VARIANTS`` (copies of
Sarabun's own glyphs, offset by Sarabun's own GPOS anchors), and ``shape()``
rewrites text to use them. ``install_tounicode_patch()`` makes reportlab map
those PUA codes back to the real characters in the PDF's ToUnicode CMap, so
copying or extracting text from the PDF still yields standard Thai.

Only the xhtml2pdf path uses this. The browser and wkhtmltopdf shape Thai
properly and get the unmodified Sarabun.
"""

# Characters ---------------------------------------------------------------

TONES = ("่", "้", "๊", "๋", "์")  # ่ ้ ๊ ๋ ์
UPPER_VOWELS = ("ั", "ิ", "ี", "ึ", "ื", "็", "ํ")
LOWER_VOWELS = ("ุ", "ู", "ฺ")  # ุ ู ฺ
SARA_AM = "ำ"  # ำ (its nikhahit ring sits where a tone mark would)
ASCENDERS = ("ป", "ฝ", "ฟ")  # ป ฝ ฟ
# Consonant -> glyph name of its form without the descender / tail, used
# when a lower vowel follows.
TAILLESS = {
    "ญ": "yoYingthai.less",  # ญ
    "ฐ": "thoThanthai.less",  # ฐ
    "ฎ": "doChadathai.short",  # ฎ
    "ฏ": "toPatakthai.short",  # ฏ
}

# Variants -----------------------------------------------------------------
# (kind, character) for every PUA glyph, in code point order from U+F700.
# kind: "left"      shifted left clear of an ascender
#       "high"      tone raised above an upper vowel / sara am
#       "high_left" both
#       "tailless"  consonant without its tail (glyph from TAILLESS)

PUA_START = 0xF700


def _variant_list():
    out = []
    for tone in TONES:
        out += [("left", tone), ("high", tone), ("high_left", tone)]
    for vowel in UPPER_VOWELS:
        out.append(("left", vowel))
    for consonant in TAILLESS:
        out.append(("tailless", consonant))
    return out


VARIANTS = _variant_list()
CODEPOINT = {variant: PUA_START + i for i, variant in enumerate(VARIANTS)}
# PUA code point -> the character it stands for (for ToUnicode).
ORIGINAL = {code: ord(ch) for (kind, ch), code in CODEPOINT.items()}


def _pua(kind, ch):
    return chr(CODEPOINT[(kind, ch)])


def shape(text):
    """
    Rewrite Thai text to Sarabun PDF's pre-positioned PUA glyphs.

    Non-Thai text is returned unchanged; so is any text when it is not a str.
    Only meaningful with the "Sarabun PDF" font: in any other font the PUA
    code points have no glyph.
    """
    if not isinstance(text, str) or not any("฀" <= c <= "๿" for c in text):
        return text
    out = []
    base = ""
    upper = False
    n = len(text)
    for i, ch in enumerate(text):
        nxt = text[i + 1] if i + 1 < n else ""
        if "ก" <= ch <= "ฮ":  # consonant
            base = ch
            upper = False
            if ch in TAILLESS and nxt in LOWER_VOWELS:
                out.append(_pua("tailless", ch))
                continue
            out.append(ch)
        elif ch in UPPER_VOWELS:
            upper = True
            out.append(_pua("left", ch) if base in ASCENDERS else ch)
        elif ch in TONES:
            # Tone written before sara am (น้ำ) also has to clear the ring.
            high = upper or nxt == SARA_AM
            if high:
                kind = "high_left" if base in ASCENDERS else "high"
            else:
                kind = "left" if base in ASCENDERS else None
            out.append(_pua(kind, ch) if kind else ch)
        elif ch in LOWER_VOWELS or ch == SARA_AM:
            out.append(ch)
        else:
            if not ("฀" <= ch <= "๿"):
                base = ""
                upper = False
            out.append(ch)
    return "".join(out)


_PATCHED = False


def install_tounicode_patch():
    """
    Make reportlab's ToUnicode CMaps map Sarabun PDF's PUA codes back to the
    Thai characters they draw. Idempotent; affects only those code points.
    """
    global _PATCHED
    if _PATCHED:
        return
    from reportlab.pdfbase import ttfonts

    original = ttfonts.makeToUnicodeCMap

    def makeToUnicodeCMap(fontname, subset):
        return original(fontname, [ORIGINAL.get(code, code) for code in subset])

    makeToUnicodeCMap.__wrapped__ = original
    ttfonts.makeToUnicodeCMap = makeToUnicodeCMap
    _PATCHED = True
