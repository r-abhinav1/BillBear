"""Split by food type with a rate card: the host types how many had each type; nobody picks."""
import time

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for

from billbear.bill.food_types import COUNTS, TYPE_LABELS, present_types, rate_card, stored_counts
from billbear.bill.money import format_money, get_currency
from billbear.bill.validation import MAX_PEOPLE, MIN_PEOPLE
from billbear import storage
from billbear.pdf import render as pdf_render
from billbear.storage import RoomError
from billbear.web.access import caller, load_room, not_linked_page, rate_limited, require_host
from billbear.web.errors import error_response
from billbear.web.observability import capture, count, distribution

bp = Blueprint("rate_card", __name__)


def _gate(room: dict):
    """The host's own page: anyone else is sent to the room, and so is a room not split this way."""
    me = caller(room)
    if not me:
        return not_linked_page(room)
    if not me["is_host"] or room["split_mode"] != COUNTS or room["finalized"]:
        return redirect(url_for("rooms.room_summary", room_code=room["code"]))
    return None


def _card(room: dict) -> tuple:
    head = room["num_people"]
    counts = stored_counts(room["type_counts"], present_types(room["items"]), head)
    return counts, rate_card(room, counts, head)


def share_text(room: dict, card: dict) -> str:
    """The rate card as a message for the group chat."""
    currency = get_currency(card["currency"])
    money = lambda value: format_money(value, currency)  # noqa: E731
    lines = [f"{room['room_name']}: {money(card['bill_total'])}"
             + (" with taxes and charges" if card["has_charges"] else ""),
             "Add up what you had:"]
    for row in card["rows"]:
        lines.append(f"{row['label']}: {money(row['each'])}")
    if card["everyone"]:
        lines.append(f"Everyone also pays: {money(card['everyone']['each'])}")
    lines += ["", "Split with BillBear"]
    return "\n".join(lines)


def _fragment(room: dict, counts: dict, card: dict) -> dict:
    """What the page redraws after the host changes a number."""
    currency = get_currency(card["currency"])
    return {
        "status": "success",
        "head": card["head"],
        "counts": counts,
        "each": {row["key"]: format_money(row["each"], currency) for row in card["rows"]},
        "html": render_template("_rate_card.html", room=room, card=card),
        "share_text": share_text(room, card),
    }


@bp.route("/room/<room_code>/rate-card", methods=["GET", "POST"])
def rate_card_page(room_code):
    room = load_room(room_code)
    if request.method == "POST":
        uid = require_host(room)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise RoomError("BAD_REQUEST", "Send the numbers as JSON.", status=400)
        head = body.get("head")
        if isinstance(head, bool) or not isinstance(head, int) or not MIN_PEOPLE <= head <= MAX_PEOPLE:
            raise RoomError(
                "BAD_COUNTS", f"The number at the table must be between {MIN_PEOPLE} and {MAX_PEOPLE}.", status=400
            )
        counts = storage.set_type_counts(room["code"], uid, head, body.get("counts"))
        room = storage.get_room(room["code"])
        return jsonify(_fragment(room, counts, rate_card(room, counts, head)))

    blocked = _gate(room)
    if blocked:
        return blocked
    counts, card = _card(room)
    return render_template(
        "rate_card.html",
        room=room,
        room_code=room["code"],
        counts=counts,
        card=card,
        labels=TYPE_LABELS,
        share_text=share_text(room, card),
        pdf_available=pdf_render.PDF_AVAILABLE,
    )


@bp.route("/room/<room_code>/rate-card/download")
def download_rate_card(room_code):
    room = load_room(room_code)
    blocked = _gate(room)
    if blocked:
        return blocked
    if not pdf_render.PDF_AVAILABLE:
        return error_response("PDF download isn't available on this server.", 503, "NO_PDF")
    if rate_limited("download"):
        return error_response("Too many downloads from this network. Wait a few minutes and try again.", 429, "RATE_LIMITED")
    _counts, card = _card(room)
    started = time.monotonic()
    try:
        data = pdf_render.render_rate_card_pdf(room, card)
        count("pdf.download", cache="none", kind="rate_card")
        distribution("pdf.render", (time.monotonic() - started) * 1000, "millisecond", outcome="ok")
        return pdf_render.pdf_response(room, data, suffix="rate_card")
    except Exception as exc:
        distribution("pdf.render", (time.monotonic() - started) * 1000, "millisecond", outcome="failed")
        current_app.logger.warning("Rate card PDF error: %s", type(exc).__name__)
        capture(exc, kind="pdf")
        return error_response("Couldn't generate the PDF. Please try again.", 500, "PDF_FAILED")
