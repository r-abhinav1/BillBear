"""The split, once the host has made it: the results page and the PDF."""
from flask import Blueprint, current_app, render_template

from billbear import storage
from billbear.bill.audit import audit_bill
from billbear.bill.food_types import TYPES, pot_lines
from billbear.bill.split import calculate_bill_split
from billbear.pdf import render as pdf_render
from billbear.web.access import caller, load_room, not_linked_page, rate_limited, redirect_for
from billbear.web.errors import error_response
from billbear.web.observability import capture, count, distribution

bp = Blueprint("results", __name__)


def _results_gate(room: dict):
    me = caller(room)
    if not me:
        return None, not_linked_page(room)
    if not room["finalized"]:
        return me, redirect_for(room, me)
    return me, None


@bp.route("/room/<room_code>/results")
def results_page(room_code):
    room = load_room(room_code)
    me, blocked = _results_gate(room)
    if blocked:
        return blocked
    bill_split = calculate_bill_split(room)
    return render_template(
        "results.html",
        room=room,
        room_code=room["code"],
        bill_split=bill_split,
        pots=pot_lines(room, bill_split) if room["split_mode"] == TYPES else None,
        pdf_available=pdf_render.PDF_AVAILABLE,
        caller_name=me["name"],
        caller_is_host=me["is_host"],
        audit_warnings=audit_bill(room, bill_split),
        ttl_hours=max(1, storage.ttl_seconds() // 3600),
    )


@bp.route("/room/<room_code>/download")
def download_pdf(room_code):
    room = load_room(room_code)
    _me, blocked = _results_gate(room)
    if blocked:
        return blocked
    if not pdf_render.PDF_AVAILABLE:
        return error_response("PDF download isn't available on this server.", 503, "NO_PDF")
    if rate_limited("download"):
        return error_response("Too many downloads from this network. Wait a few minutes and try again.", 429, "RATE_LIMITED")
    import time

    started = time.monotonic()
    try:
        data, hit = pdf_render.pdf_bytes(room, room["code"])
        count("pdf.download", cache="hit" if hit else "miss")
        if not hit:
            distribution("pdf.render", (time.monotonic() - started) * 1000, "millisecond", outcome="ok")
        return pdf_render.pdf_response(room, data)
    except Exception as exc:
        distribution("pdf.render", (time.monotonic() - started) * 1000, "millisecond", outcome="failed")
        current_app.logger.warning("PDF generation error: %s", type(exc).__name__)
        capture(exc, kind="pdf")
        return error_response("Couldn't generate the PDF. Please try again.", 500, "PDF_FAILED")
