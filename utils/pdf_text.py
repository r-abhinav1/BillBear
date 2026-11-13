"""
Non-Latin text in the PDF: Hindi, Tamil, Japanese and so on.

The PDF is drawn by xhtml2pdf with the PDF's built-in Helvetica and Courier,
which only have Western European letters, so 醤油ラーメン or पनीर टिक्का came
out as ■■■. Two things fix that:

1. Fonts. `pdf_text` splits a name into runs by script and wraps each
   non-Latin run in a span with a bundled font for that script (in
   static/fonts/pdf). Latin text and every amount stay in Helvetica, so a bill
   in English looks exactly as before. xhtml2pdf can't fall back from one font
   to another per character, hence the splitting; it also means the CJK font
   needn't have Latin digits (it doesn't).

2. Shaping. Indian scripts join letters (क + ् + ष = क्ष) and move vowel signs
   (कि), which needs HarfBuzz. ReportLab can shape, but xhtml2pdf draws text
   through its own old copy of ReportLab's paragraph code, which never asks
   for it. `install_shaping` shapes those runs at the last step instead, just
   before ReportLab writes them out. Without the uharfbuzz package the text
   still prints, with the letters unjoined.
"""
import os
import re
from typing import Iterable, List, Optional, Set, Tuple

from markupsafe import Markup, escape

FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "fonts", "pdf")

# CSS family (what the template names) -> file, and the code points it covers.
# ₹ has no glyph in Helvetica; the Devanagari font has one.
SCRIPT_FONTS: List[Tuple[str, str, List[Tuple[int, int]]]] = [
    ("bbdeva", "NotoSansDevanagari-Regular.ttf", [(0x0900, 0x097F), (0xA8E0, 0xA8FF), (0x20B9, 0x20B9)]),
    ("bbbeng", "NotoSansBengali-Regular.ttf", [(0x0980, 0x09FF)]),
    ("bbguru", "NotoSansGurmukhi-Regular.ttf", [(0x0A00, 0x0A7F)]),
    ("bbgujr", "NotoSansGujarati-Regular.ttf", [(0x0A80, 0x0AFF)]),
    ("bborya", "NotoSansOriya-Regular.ttf", [(0x0B00, 0x0B7F)]),
    ("bbtaml", "NotoSansTamil-Regular.ttf", [(0x0B80, 0x0BFF)]),
    ("bbtelu", "NotoSansTelugu-Regular.ttf", [(0x0C00, 0x0C7F)]),
    ("bbknda", "NotoSansKannada-Regular.ttf", [(0x0C80, 0x0CFF)]),
    ("bbmlym", "NotoSansMalayalam-Regular.ttf", [(0x0D00, 0x0D7F)]),
    ("bbsinh", "NotoSansSinhala-Regular.ttf", [(0x0D80, 0x0DFF)]),
    ("bbthai", "NotoSansThai-Regular.ttf", [(0x0E00, 0x0E7F)]),
    ("bbcjk", "DroidSansFallbackFull.ttf", [
        (0x1100, 0x11FF), (0x2E80, 0x2FDF), (0x3000, 0x33FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF),
        (0xA960, 0xA97F), (0xAC00, 0xD7FF), (0xF900, 0xFAFF), (0xFE30, 0xFE4F), (0xFF00, 0xFFEF),
    ]),
]
_FILES = {family: filename for family, filename, _ranges in SCRIPT_FONTS}
# Scripts whose letters join or reorder; CJK, Latin and digits never need it.
_NEEDS_SHAPING = re.compile("[ऀ-๿꣠-ꣿ]")
# Zero-width joiners steer how letters join, so they stay in the run they're in.
_JOINERS = {"‌", "‍"}


def family_of(ch: str) -> Optional[str]:
    """The bundled font family for a character, or None for Helvetica's own."""
    cp = ord(ch)
    if cp < 0x0900:
        return None
    for family, _filename, ranges in SCRIPT_FONTS:
        for low, high in ranges:
            if low <= cp <= high:
                return family
    return None


def runs(text: str) -> List[Tuple[Optional[str], str]]:
    """'Paneer पनीर 2' -> [(None, 'Paneer '), ('bbdeva', 'पनीर'), (None, ' 2')]"""
    result: List[Tuple[Optional[str], str]] = []
    for ch in text:
        family = result[-1][0] if (ch in _JOINERS and result) else family_of(ch)
        if result and result[-1][0] == family:
            result[-1] = (family, result[-1][1] + ch)
        else:
            result.append((family, ch))
    return result


def pdf_text(value) -> Markup:
    """Jinja filter: escaped text, with each non-Latin run in its script's font."""
    text = "" if value is None else str(value)
    parts = []
    for family, chunk in runs(text):
        if family is None:
            parts.append(str(escape(chunk)))
        else:
            parts.append(f'<span style="font-family: {family}">{escape(chunk)}</span>')
    return Markup("".join(parts))


def families_in(texts: Iterable[str]) -> Set[str]:
    """Which bundled fonts a document needs (only those are loaded: the CJK one is 4 MB)."""
    found: Set[str] = set()
    for text in texts:
        for ch in str(text or ""):
            family = family_of(ch)
            if family:
                found.add(family)
    return found


def font_face_css(families: Iterable[str], prefix: str = "bbfont/") -> Markup:
    """@font-face rules for the given families; `prefix` is resolved like bbasset/ images."""
    rules = [
        f"@font-face {{ font-family: {family}; src: url('{prefix}{_FILES[family]}'); }}"
        for family in sorted(families) if family in _FILES
    ]
    return Markup("\n".join(rules))


def font_path(filename: str) -> Optional[str]:
    """The bundled font file a bbfont/ URL names, or None for anything else."""
    return os.path.join(FONT_DIR, filename) if filename in _FILES.values() else None


_installed = False


def install_shaping() -> bool:
    """
    Shape Indian-script and Thai runs as ReportLab writes them. Safe to call
    on every PDF; returns False (text unjoined but readable) without uharfbuzz.
    """
    global _installed
    if _installed:
        return True
    try:
        import uharfbuzz  # noqa: F401
        from reportlab.pdfbase.pdfmetrics import getFont
        from reportlab.pdfbase.ttfonts import ShapedStr, shapeStr
        from reportlab.pdfgen.textobject import PDFTextObject
    except ImportError:
        return False

    original = PDFTextObject._formatText

    def _formatText(self, text):
        if isinstance(text, str) and not isinstance(text, ShapedStr) and _NEEDS_SHAPING.search(text):
            try:
                if getattr(getFont(self._fontname), "shapable", False):
                    text = shapeStr(text, self._fontname, self._fontsize)
            except Exception:  # never lose the PDF over a shaping problem
                pass
        return original(self, text)

    PDFTextObject._formatText = _formatText
    _installed = True
    return True
