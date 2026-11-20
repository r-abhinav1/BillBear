"""The host's controls: manage guests, the head count, reopen, fix the bill, split it."""
from datetime import datetime

from flask import Blueprint, jsonify, request, url_for

from billbear import storage
from billbear.bill.food_types import COUNTS
from billbear.bill.validation import MAX_PEOPLE, MIN_PEOPLE
from billbear.storage import RoomError
from billbear.web.access import load_room, require_host
from billbear.web.observability import count, distribution
from billbear.web.sharing import share_url

bp = Blueprint("host", __name__)


def _json_body() -> dict:
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    return request.form.to_dict()


@bp.route("/room/<room_code>/host/remove-user", methods=["POST"])
def host_remove_user(room_code):
    room = load_room(room_code)
    uid = require_host(room)
    target = str(_json_body().get("target_user", "")).strip()
    if not target:
        raise RoomError("BAD_REQUEST", "Missing target_user", status=400)
    removed = storage.remove_participant(room["code"], uid, target)
    return jsonify({"status": "success", "removed": removed})


@bp.route("/room/<room_code>/host/reset-device", methods=["POST"])
def host_reset_device(room_code):
    room = load_room(room_code)
    uid = require_host(room)
    target = str(_json_body().get("target_user", "")).strip()
    if not target:
        raise RoomError("BAD_REQUEST", "Missing target_user", status=400)
    name, move_code = storage.reset_device(room["code"], uid, target)
    # Only the host sees the code; the link fills in the name and the code on the new phone.
    move_link = share_url("join.join_room", room_code=room["code"], name=name, move=move_code)
    return jsonify({"status": "success", "reset": name, "move_code": move_code, "move_link": move_link})


@bp.route("/room/<room_code>/host/num-people", methods=["POST"])
def host_update_num_people(room_code):
    room = load_room(room_code)
    uid = require_host(room)
    try:
        value = int(str(_json_body().get("num_people", "")).strip())
    except (TypeError, ValueError):
        raise RoomError("BAD_REQUEST", "num_people must be an integer", status=400)
    if not MIN_PEOPLE <= value <= MAX_PEOPLE:
        raise RoomError("BAD_REQUEST", f"Number of people must be between {MIN_PEOPLE} and {MAX_PEOPLE}.", status=400)
    storage.set_num_people(room["code"], uid, value)
    return jsonify({"status": "success", "num_people": value})


@bp.route("/room/<room_code>/host/reopen-selections", methods=["POST"])
def host_reopen_selections(room_code):
    room = load_room(room_code)
    uid = require_host(room)
    storage.reopen_all(room["code"], uid)
    count("picks.reopened")
    return jsonify({"status": "success"})


@bp.route("/room/<room_code>/host/fix-bill", methods=["POST"])
def host_fix_bill(room_code):
    """Results -> Check the bill, keeping everyone's picks (see storage.unfinalize)."""
    room = load_room(room_code)
    uid = require_host(room)
    storage.unfinalize(room["code"], uid)
    count("bill.fix")
    return jsonify({"status": "success", "redirect": url_for("rooms.edit_items", room_code=room["code"])})


@bp.route("/room/<room_code>/host/split-mode", methods=["POST"])
def host_split_mode(room_code):
    """Switch how the bill is split. {"mode": "items"|"types"|"counts", "reopen": true to send everyone back}."""
    room = load_room(room_code)
    uid = require_host(room)
    body = _json_body()
    mode = body.get("mode")
    if not isinstance(mode, str):
        raise RoomError("BAD_REQUEST", "Missing mode", status=400)
    reopen = body.get("reopen") is True or body.get("reopen") == "1"
    result = storage.set_split_mode(room["code"], uid, mode.strip(), reopen=reopen)
    count("split_mode.chosen", mode=result["mode"], where="room", reopened=bool(result["reopened"]))
    target = (url_for("rate_card.rate_card_page", room_code=room["code"]) if result["mode"] == COUNTS
              else url_for("rooms.room_summary", room_code=room["code"]))
    return jsonify({"status": "success", **result, "redirect": target})


@bp.route("/room/<room_code>/host/revoke/<target_user>", methods=["POST"])
def host_revoke_user(room_code, target_user):
    room = load_room(room_code)
    uid = require_host(room)
    storage.host_revoke(room["code"], uid, target_user)
    return jsonify({"status": "success", "revoked": target_user})


@bp.route("/room/<room_code>/finalize", methods=["POST"])
def finalize(room_code):
    room = load_room(room_code)
    uid = require_host(room)
    result = storage.finalize_room(room["code"], uid)
    if not result["already"]:
        count("bill.split", currency=room["currency"]["code"], people=len(room["users"]))
        if room.get("created_at"):
            minutes = (datetime.now(room["created_at"].tzinfo) - room["created_at"]).total_seconds() / 60
            distribution("bill.minutes_to_split", minutes, "minute")
    return jsonify({
        "status": "success",
        "already_finalized": result["already"],
        "redirect": url_for("results.results_page", room_code=room["code"]),
    })
