"""The split as a PDF, rendered once per split and cached."""
import os
import re
import threading
from collections import OrderedDict
from datetime import datetime
from io import BytesIO

from flask import make_response, render_template

from billbear.bill.audit import audit_bill
from billbear.bill.food_types import TYPES, pot_lines
from billbear.bill.split import calculate_bill_split
from billbear.config import on_vercel, STATIC_DIR
from billbear.pdf.text import families_in, FONT_DIR, font_face_css, font_path, install_shaping

# Which PDF library this server has: WeasyPrint locally if installed, else xhtml2pdf (always on Vercel).
PDF_AVAILABLE = False
WEASYPRINT_AVAILABLE = False
XHTML2PDF_AVAILABLE = False

if not on_vercel():
    try:
        import weasyprint
        WEASYPRINT_AVAILABLE = True
        PDF_AVAILABLE = True
    except Exception as exc:
        print(f"WeasyPrint not available, trying xhtml2pdf... ({exc})")

if not PDF_AVAILABLE:
    try:
        from xhtml2pdf import pisa
        XHTML2PDF_AVAILABLE = True
        PDF_AVAILABLE = True
    except Exception as exc:
        print(f"xhtml2pdf not available ({exc})")


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 _-]+", "", name).strip().replace(" ", "_")
    return (cleaned or "bill")[:50]


# Images the PDF template may embed, referenced there as "bbasset/<name>".
_PDF_ASSET_DIR = str(STATIC_DIR / "images")


# The watermark is a JPEG on purpose: it is drawn on every page, and the PDF
# library stores a PNG as raw pixels each time (~135 KB a page) but passes a
# JPEG through as is (~7 KB). Vercel can't send a response over 4.5 MB.
_PDF_ASSETS = {
    "pdf-bear.png", "pdf-watermark.jpg",
    "pdf-veg.png", "pdf-nonveg.png", "pdf-egg.png", "pdf-alcohol.png", "pdf-blank.png",
}


def _pdf_link_callback(uri: str, rel: str) -> str:
    """Resolve our own bundled images and fonts; block every other resource the HTML names."""
    prefix = "bbasset/"
    if uri.startswith(prefix) and uri[len(prefix):] in _PDF_ASSETS:
        return os.path.join(_PDF_ASSET_DIR, uri[len(prefix):])
    if uri.startswith("bbfont/"):
        return font_path(uri[len("bbfont/"):]) or ""
    return ""


def _pdf_fonts(room: dict, bill_split: dict):
    """@font-face rules for the scripts this room's names and labels use, and nothing more."""
    texts = [room["room_name"], room["host_name"], *bill_split["user_breakdown"].keys()]
    texts += [item["name"] for item in room["items"]]
    texts += [c["label"] for c in bill_split["totals"]["charges"]]
    return font_face_css(families_in(texts))


# Rendered PDFs, newest last. Everyone at a table downloads the same file, and
# rendering is the most CPU-hungry thing the app does (~0.8 s on Vercel), so
# each split is rendered once per instance. The key includes when the bill was
# split: Fix the bill or reopening and splitting again makes a new key, so a
# stale PDF is never served. A finalized room can't otherwise change.
_PDF_CACHE: "OrderedDict[tuple, bytes]" = OrderedDict()


_PDF_CACHE_LOCK = threading.Lock()


_PDF_CACHE_MAX = 32          # ~300 KB each


def _pdf_cache_key(room: dict) -> tuple:
    finalized = room.get("finalized_at")
    return (room["code"], finalized.isoformat() if finalized else None)


def pdf_bytes(room: dict, room_code: str) -> tuple:
    """The split's PDF as bytes, and whether it came from the cache."""
    key = _pdf_cache_key(room)
    with _PDF_CACHE_LOCK:
        cached = _PDF_CACHE.get(key)
        if cached is not None:
            _PDF_CACHE.move_to_end(key)
            return cached, True
    data = render_pdf(room, room_code)
    with _PDF_CACHE_LOCK:
        _PDF_CACHE[key] = data
        _PDF_CACHE.move_to_end(key)
        while len(_PDF_CACHE) > _PDF_CACHE_MAX:
            _PDF_CACHE.popitem(last=False)
    return data, False


def pdf_response(room: dict, data: bytes, suffix: str = "bill_split"):
    response = make_response(data)
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = f'attachment; filename="{_safe_filename(room["room_name"])}_{suffix}.pdf"'
    return response


def _html_to_pdf(html: str) -> bytes:
    install_shaping()
    buf = BytesIO()
    if WEASYPRINT_AVAILABLE and not on_vercel():
        from pathlib import Path

        html = html.replace("bbasset/", Path(_PDF_ASSET_DIR).as_uri() + "/")
        html = html.replace("bbfont/", Path(FONT_DIR).as_uri() + "/")
        weasyprint.HTML(string=html).write_pdf(buf)
    elif XHTML2PDF_AVAILABLE:
        status = pisa.CreatePDF(html, dest=buf, link_callback=_pdf_link_callback)
        if status.err:
            raise RuntimeError("xhtml2pdf failed")
    else:
        raise RuntimeError("No PDF library")
    return buf.getvalue()


def render_rate_card_pdf(room: dict, card: dict) -> bytes:
    """A rate card (split by food type, host's counts) as a PDF. Not cached: the counts can change."""
    texts = [room["room_name"], room["host_name"]] + [item["name"] for item in room["items"]]
    html = render_template(
        "rate_card_pdf.html",
        room=room,
        room_code=room["code"],
        card=card,
        current_date=datetime.now(),
        font_faces=font_face_css(families_in(texts)),
    )
    return _html_to_pdf(html)


def render_pdf(room: dict, room_code: str) -> bytes:
    bill_split = calculate_bill_split(room)
    all_square = all(w.get("severity") in ("ok", "note") for w in audit_bill(room, bill_split))
    html = render_template(
        "results_pdf.html",
        room=room,
        room_code=room_code,
        bill_split=bill_split,
        # Split by food type: each person's lines are their pots, not every dish.
        pots=pot_lines(room, bill_split) if room.get("split_mode") == TYPES else None,
        all_square=all_square,
        # When the bill was split (in the server's time zone, as before), not when
        # this copy was made: every download of one split is the same file.
        current_date=room["finalized_at"].astimezone() if room.get("finalized_at") else datetime.now(),
        font_faces=_pdf_fonts(room, bill_split),
    )
    return _html_to_pdf(html)
