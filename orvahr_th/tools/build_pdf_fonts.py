"""
Build "Sarabun PDF" (SarabunPDF-Regular.ttf / SarabunPDF-Bold.ttf) from Sarabun.

Not run by the app: the output is committed next to the source fonts in
orvahr_th/static/orvahr_th/fonts/. Re-run only if Sarabun is updated or the
variant table in orvahr_th/thai_shaping.py changes. Needs fontTools, which is
not an app dependency:

    uv venv /tmp/fontvenv && uv pip install --python /tmp/fontvenv/bin/python fonttools
    /tmp/fontvenv/bin/python orvahr_th/tools/build_pdf_fonts.py

What it does: adds one glyph per entry of thai_shaping.VARIANTS,
mapped in the cmap to its PUA code point. Each is a copy of Sarabun's own
glyph outline, offset by the amount Sarabun's own GPOS anchors would have moved it
(mark-to-base for the ascender shift, mark-to-mark for a tone over an upper
vowel). Nothing in the existing glyphs, metrics or tables is changed other
than the cmap, the glyph tables and the names.

Licence: Sarabun is SIL OFL 1.1 with no Reserved Font Name; the derived font
is renamed "Sarabun PDF" anyway and ships under the same licence (OFL.txt).
"""

import importlib.util
import os
import sys

from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
FONTS = os.path.join(APP, "static", "orvahr_th", "fonts")


def _load_shaping():
    spec = importlib.util.spec_from_file_location(
        "thai_shaping", os.path.join(APP, "thai_shaping.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _subtables(lookup):
    for sub in lookup.SubTable:
        yield sub.ExtSubTable if lookup.LookupType == 9 else sub


def _type(lookup):
    if lookup.LookupType == 9:
        return lookup.SubTable[0].ExtSubTable.LookupType
    return lookup.LookupType


def ascender_shift(font, mark, bases):
    """
    x offset GPOS mark-to-base applies to ``mark`` on the ascender ``bases``,
    relative to where reportlab draws it (the base's advance + 0).
    Most negative wins, so the mark clears every ascender.
    """
    hmtx = font["hmtx"]
    shifts = []
    for lookup in font["GPOS"].table.LookupList.Lookup:
        if _type(lookup) != 4:
            continue
        for st in _subtables(lookup):
            marks = st.MarkCoverage.glyphs
            if mark not in marks:
                continue
            record = st.MarkArray.MarkRecord[marks.index(mark)]
            for base in bases:
                if base not in st.BaseCoverage.glyphs:
                    continue
                anchor = st.BaseArray.BaseRecord[
                    st.BaseCoverage.glyphs.index(base)
                ].BaseAnchor[record.Class]
                if anchor is None:
                    continue
                default_x = hmtx[base][0] + record.MarkAnchor.XCoordinate
                shifts.append(anchor.XCoordinate - default_x)
    if not shifts:
        raise SystemExit(f"no mark-to-base anchor for {mark}")
    return min(shifts)


def tone_raise(font, tone, vowels):
    """y offset GPOS mark-to-mark gives ``tone`` over the tallest of ``vowels``."""
    raises = []
    for lookup in font["GPOS"].table.LookupList.Lookup:
        if _type(lookup) != 6:
            continue
        for st in _subtables(lookup):
            m1 = st.Mark1Coverage.glyphs
            if tone not in m1:
                continue
            record = st.Mark1Array.MarkRecord[m1.index(tone)]
            for vowel in vowels:
                if vowel not in st.Mark2Coverage.glyphs:
                    continue
                anchor = st.Mark2Array.Mark2Record[
                    st.Mark2Coverage.glyphs.index(vowel)
                ].Mark2Anchor[record.Class]
                if anchor is not None:
                    raises.append(
                        anchor.YCoordinate - record.MarkAnchor.YCoordinate
                    )
    if not raises:
        raise SystemExit(f"no mark-to-mark anchor for {tone}")
    return max(raises)


def offset_copy(font, source, dx, dy):
    """
    A plain (non-composite) copy of ``source`` moved by (dx, dy).

    Not a composite glyph: reportlab 4.0's subsetter mis-maps a composite's
    component when the same base glyph is also used directly, which drew the
    shifted mark unshifted.
    """
    glyph_set = font.getGlyphSet()
    pen = TTGlyphPen(glyph_set)
    glyph_set[source].draw(TransformPen(pen, (1, 0, 0, 1, dx, dy)))
    return pen.glyph()


def build(weight, shaping):
    src = os.path.join(FONTS, f"Sarabun-{weight}.ttf")
    dst = os.path.join(FONTS, f"SarabunPDF-{weight}.ttf")
    font = TTFont(src)
    cmap = font.getBestCmap()
    name = {ch: cmap[ord(ch)] for ch in shaping.TONES + shaping.UPPER_VOWELS}
    ascenders = [cmap[ord(c)] for c in shaping.ASCENDERS]
    upper = [cmap[ord(c)] for c in shaping.UPPER_VOWELS if c != "็"]

    glyf, hmtx = font["glyf"], font["hmtx"]
    order = list(font.getGlyphOrder())
    new_cmap = {}
    for (kind, ch), code in shaping.CODEPOINT.items():
        glyph_name = f"uni{code:04X}"
        if kind == "tailless":
            source = shaping.TAILLESS[ch]
            dx = dy = 0
        else:
            source = name[ch]
            dx = ascender_shift(font, source, ascenders) if "left" in kind else 0
            dy = tone_raise(font, source, upper) if "high" in kind else 0
        glyph = offset_copy(font, source, dx, dy)
        glyph.recalcBounds(glyf)
        glyf[glyph_name] = glyph
        # The left side bearing must match the moved outline: rasterisers
        # (FreeType, MuPDF) place a glyph by its hmtx lsb, so keeping the
        # source's lsb silently undoes a horizontal shift.
        hmtx[glyph_name] = (hmtx[source][0], glyph.xMin)
        order.append(glyph_name)
        new_cmap[code] = glyph_name
        print(f"  U+{code:04X} {kind:9} U+{ord(ch):04X} <- {source} ({dx:+}, {dy:+})")
    font.setGlyphOrder(order)
    glyf.glyphOrder = order
    for table in font["cmap"].tables:
        if table.isUnicode():
            table.cmap.update(new_cmap)
    font["maxp"].numGlyphs = len(order)
    # The DSIG no longer matches; drop it rather than ship a bad signature.
    if "DSIG" in font:
        del font["DSIG"]
    for record in font["name"].names:
        text = record.toUnicode()
        if "Sarabun" in text and record.nameID in (1, 3, 4, 6, 16):
            new = "SarabunPDF" if record.nameID == 6 else "Sarabun PDF"
            record.string = text.replace("Sarabun", new, 1)
    font.save(dst)
    print(f"wrote {dst}")


def main():
    shaping = _load_shaping()
    for weight in ("Regular", "Bold"):
        print(weight)
        build(weight, shaping)


if __name__ == "__main__":
    sys.exit(main())
