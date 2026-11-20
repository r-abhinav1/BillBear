"""Joining a room, the host's recovery link, and finding your own page again."""
from flask import Blueprint, make_response, redirect, render_template, request, url_for

from billbear import storage
from billbear.bill.validation import normalize_person_name
from billbear.storage import RoomError
from billbear.web.access import caller, load_room, miss_blocked, not_linked_page, rate_limited, redirect_for
from billbear.web.observability import count
from billbear.web.session import set_hostkey_cookie, set_session_cookie

bp = Blueprint("join", __name__)


def _do_join(code: str, raw_name: str, move_code: str = ""):
    """Shared join logic. Returns a response on success, raises ValueError/RoomError otherwise."""
    name = normalize_person_name(raw_name)
    if rate_limited("join"):
        raise RoomError("RATE_LIMITED", "Too many join attempts. Wait a few minutes.", status=429)
    status, row, uid = storage.join_participant(code, name, move_code)
    print(f"[join] {status} room={code}")
    count("room.joined", result=status.lower())
    response = make_response(redirect(url_for("join.user_room", room_code=code, user_name=row["name"])))
    set_session_cookie(response, code, uid, row["name"])
    return response


@bp.route("/join", methods=["GET", "POST"])
def join():
    if request.method == "GET":
        return render_template("join_room.html")
    code = storage.normalize_code(request.form.get("room_code", ""))
    if not code:
        return render_template("join_room.html", error="Please enter a room code"), 400
    try:
        room = load_room(code)
    except RoomError as exc:
        return render_template("join_room.html", error=exc.message), exc.status
    me = caller(room)
    if me:
        return redirect_for(room, me)
    try:
        return _do_join(room["code"], request.form.get("user_name"))
    except RoomError as exc:
        if exc.code in MOVE_CODE_ERRORS:
            # The code form has no move-code field: show the room's own join form, which does.
            return _join_direct_page(room, exc)
        return render_template("join_room.html", error=exc.message), exc.status
    except ValueError as exc:
        return render_template("join_room.html", error=str(exc)), 400


MOVE_CODE_ERRORS = {"NEEDS_MOVE_CODE", "BAD_MOVE_CODE"}


def _join_direct_page(room: dict, exc):
    """The room's join form again, with the error; the move-code field shows when a reset name needs it."""
    message = exc.message if isinstance(exc, RoomError) else str(exc)
    return render_template(
        "join_room_direct.html",
        error=message,
        room=room,
        room_code=room["code"],
        need_move_code=isinstance(exc, RoomError) and exc.code in MOVE_CODE_ERRORS,
        prefill_name=request.form.get("user_name", ""),
        prefill_move="",
    ), getattr(exc, "status", 400)


@bp.route("/join/<room_code>", methods=["GET", "POST"])
def join_room(room_code):
    try:
        room = load_room(room_code)
    except RoomError as exc:
        if exc.code in {"NOT_FOUND", "RATE_LIMITED"}:
            return render_template(
                "join_room_direct.html", error=exc.message, room_code=storage.normalize_code(room_code)
            ), exc.status
        raise
    code = room["code"]
    me = caller(room)
    if me:
        return redirect_for(room, me)
    if request.method == "POST":
        try:
            return _do_join(code, request.form.get("user_name"), request.form.get("move_code", ""))
        except (ValueError, RoomError) as exc:
            return _join_direct_page(room, exc)
    # The host's "send them this link" fills both fields in. A GET never joins,
    # so a link preview bot opening it takes nothing.
    move = storage.normalize_move_code(request.args.get("move"))[:storage.MOVE_CODE_LENGTH]
    page = dict(
        room=room,
        room_code=code,
        need_move_code=bool(move),
        prefill_name=request.args.get("name", "")[:30],
        prefill_move=move,
    )
    if room["finalized"] and not move:
        page["error"] = (
            "This bill has already been finalized. If you were part of it, ask the host to move you "
            "to a new phone (Manage, next to your name), then join with the same name and the move code."
        )
    return render_template("join_room_direct.html", **page)


@bp.route("/room/<room_code>/host", methods=["GET", "POST"])
def host_recover(room_code):
    """
    Host recovery link. GET only shows a confirm button: link-preview bots
    (WhatsApp, Slack) fetch URLs, and a GET that rotated the host device would
    log the real host out.
    """
    room = load_room(room_code)
    code = room["code"]
    token = request.values.get("k", "")
    if request.method == "GET":
        return render_template("host_recover.html", room=room, room_code=code, token=token)
    # load_room lets room members past the guess limiter, but token guessing must stay throttled for everyone.
    if miss_blocked():
        raise RoomError("RATE_LIMITED", "Too many attempts. Wait a few minutes and try again.", status=429)
    try:
        uid = storage.recover_host(code, token)
    except RoomError as exc:
        if exc.code == "BAD_TOKEN":
            rate_limited("miss")
        raise
    response = make_response(redirect(url_for("rooms.room_summary", room_code=code)))
    set_session_cookie(response, code, uid, room["host_name"])
    set_hostkey_cookie(response, code, token)
    return response


@bp.route("/room/<room_code>/user/<user_name>")
def user_room(room_code, user_name):
    room = load_room(room_code)
    me = caller(room)
    if not me:
        return not_linked_page(room)
    return redirect_for(room, me)
