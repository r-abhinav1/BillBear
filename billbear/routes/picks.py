"""Picking dishes, changing your mind, and waiting for the others."""
from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from billbear import storage
from billbear.bill.food_types import COUNTS, TYPES, pots, public_pots
from billbear.storage import RoomError
from billbear.web.access import caller, load_room, not_linked_page, redirect_for, require_caller
from billbear.web.errors import wants_json
from billbear.web.observability import count
from billbear.web.sharing import generate_qr_base64, host_recovery_url, share_url

bp = Blueprint("picks", __name__)


def _resolve_item_ids(room: dict, values: list) -> list:
    """Checkbox values are item ids; plain names are accepted from pages rendered before the upgrade."""
    by_display = {i["name"]: i["id"] for i in room["items"]}
    raw_counts = {}
    for item in room["items"]:
        raw_counts[item["raw_name"]] = raw_counts.get(item["raw_name"], 0) + 1
    ids = []
    for value in values:
        value = (value or "").strip()
        if value.isdigit():
            ids.append(int(value))
        elif value in by_display and raw_counts.get(value, 0) <= 1:
            ids.append(by_display[value])
        else:
            raise RoomError("ITEMS_CHANGED", "The item list changed. Reload the page and pick again.", status=409)
    return ids


@bp.route("/room/<room_code>/user/<user_name>/select-items", methods=["GET", "POST"])
def select_items(room_code, user_name):
    room = load_room(room_code)
    code = room["code"]

    if request.method == "POST":
        me, uid = require_caller(room)
        if me["name"] != user_name:
            raise RoomError("NOT_YOUR_SESSION", "You can only submit your own selection.", status=403)
        target = url_for("picks.waiting_room", room_code=code, user_name=me["name"])
        # The pick page says which way it was made for; storage refuses it if the
        # host has switched since (MODE_CHANGED), so a page left open never sends
        # dishes into a food-type split or the other way round.
        if request.form.get("split_mode") == TYPES:
            result = storage.submit_types(
                code, uid, request.form.getlist("selected_types"), request.form.get("submission_id", "").strip()
            )
            count("picks.sent", repeat=bool(result["idempotent"]), empty=not result["types"], mode=TYPES)
            if wants_json():
                return jsonify({"status": "success", "idempotent": result["idempotent"], "redirect": target})
            return redirect(target)
        item_ids = _resolve_item_ids(room, request.form.getlist("selected_items"))
        # "portions_<item id>": whole units of a several-of dish the caller had to
        # themselves; 0 or absent means they are sharing whatever nobody claims.
        portions = {}
        for item_id in item_ids:
            raw = request.form.get(f"portions_{item_id}", "").strip()
            if raw.isdigit():
                portions[item_id] = min(int(raw), 99)
        result = storage.submit_selection(
            code, uid, item_ids, request.form.get("submission_id", "").strip(), portions
        )
        count("picks.sent", repeat=bool(result["idempotent"]), empty=not item_ids)
        if wants_json():
            return jsonify({"status": "success", "idempotent": result["idempotent"], "redirect": target})
        return redirect(target)

    me = caller(room)
    if not me:
        return not_linked_page(room)
    if room["split_mode"] == COUNTS and not room["finalized"]:
        # A rate card has nothing to pick.
        if me["is_host"]:
            return redirect(url_for("rate_card.rate_card_page", room_code=code))
        return redirect(url_for("rooms.room_summary", room_code=code))
    if me["name"] != user_name or room["finalized"] or me["submitted"]:
        return redirect_for(room, me)
    if room["split_mode"] == TYPES:
        return render_template(
            "select_types.html",
            room=room,
            room_code=code,
            user_name=me["name"],
            pots=pots(room["items"]),
            pre_selected=me["categories"],
        )
    return render_template(
        "select_items.html",
        room=room,
        room_code=code,
        user_name=me["name"],
        pre_selected=me["claim_ids"],
        pre_portions=me["claim_portions"],
        pre_lowered=me["claim_lowered"],
    )


@bp.route("/api/room/<room_code>/my-status")
def my_status(room_code):
    room = load_room(room_code)
    me = caller(room)
    if not me:
        return jsonify({"status": "success", "authenticated": False, "in_room": False, "submitted": False})
    names = {i["id"]: i["name"] for i in room["items"]}
    if room["finalized"]:
        target = url_for("results.results_page", room_code=room["code"])
    elif me["submitted"]:
        target = url_for("picks.waiting_room", room_code=room["code"], user_name=me["name"])
    else:
        target = url_for("picks.select_items", room_code=room["code"], user_name=me["name"])
    return jsonify({
        "status": "success",
        "authenticated": True,
        "user_name": me["name"],
        "in_room": True,
        "submitted": me["submitted"],
        "selections": [names[i] for i in me["claim_ids"] if i in names],
        "submission_id": me["submission_id"],
        "redirect": target,
    })


@bp.route("/room/<room_code>/waiting")
@bp.route("/room/<room_code>/waiting/<user_name>")
def waiting_room(room_code, user_name=None):
    room = load_room(room_code)
    me = caller(room)
    if not me:
        return not_linked_page(room)
    if room["finalized"]:
        return redirect(url_for("results.results_page", room_code=room["code"]))
    if room["split_mode"] == COUNTS:
        if me["is_host"]:
            return redirect(url_for("rate_card.rate_card_page", room_code=room["code"]))
        return redirect(url_for("rooms.room_summary", room_code=room["code"]))
    if user_name != me["name"]:
        return redirect(url_for("picks.waiting_room", room_code=room["code"], user_name=me["name"]))
    join_url = share_url("join.join_room", room_code=room["code"])
    return render_template(
        "waiting_room.html",
        room=room,
        pots=public_pots(room["items"]),
        room_code=room["code"],
        user_name=me["name"],
        is_host=me["is_host"],
        qr_b64=generate_qr_base64(join_url),
        join_url=join_url,
        host_link=host_recovery_url(room["code"]) if me["is_host"] else None,
    )


@bp.route("/room/<room_code>/user/<user_name>/revoke", methods=["POST"])
def revoke_own_selection(room_code, user_name):
    room = load_room(room_code)
    me, uid = require_caller(room)
    if me["name"] != user_name:
        raise RoomError("NOT_YOUR_SESSION", "Not your session.", status=403)
    storage.revoke_own(room["code"], uid)
    return jsonify({
        "status": "success",
        "redirect": url_for("picks.select_items", room_code=room["code"], user_name=me["name"]),
    })
