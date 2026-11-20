"""Who is calling: loading a room, the caller's seat in it, and rate limits per network."""
import re

from flask import g, redirect, render_template, request, url_for

from billbear import storage
from billbear.storage import RoomError
from billbear.web.security import client_ip, RATE_LIMITS
from billbear.web.session import read_session_cookie


def rate_limited(name: str) -> bool:
    """Count a hit; True if the caller is over the limit."""
    if g.get("load_test") and not name.startswith("scan"):
        return False      # scans keep their limits: each one is a paid model call
    limit, window = RATE_LIMITS[name]
    return not storage.rate_limit_hit(f"{name}:{client_ip(request)}", limit, window)


def miss_blocked() -> bool:
    limit, window = RATE_LIMITS["miss"]
    return not storage.rate_limit_peek(f"miss:{client_ip(request)}", limit, window)


def load_room(code: str) -> dict:
    """
    Load an alive room or raise. Strangers who keep missing get a 429 (code guessing).

    Someone holding a valid session cookie for this exact code is neither counted
    nor blocked: they already know the code, and a cookie for one room is useless
    for guessing another. Without this, one noisy person on shared Wi-Fi would lock
    everyone else on that network out of their own rooms.
    """
    code = storage.normalize_code(code)
    knows_code = bool(caller_uid(code))
    if not knows_code and miss_blocked():
        raise RoomError("RATE_LIMITED", "Too many attempts. Wait a few minutes and try again.", status=429)
    room = storage.get_room(code) if re.fullmatch(r"[A-Z0-9]{6}", code) else None
    if room is None:
        if not knows_code:
            rate_limited("miss")
        raise RoomError("NOT_FOUND", "This room doesn't exist or has expired.", status=404)
    return room


def caller_uid(code: str):
    payload = read_session_cookie(request, code)
    return payload.get("uid") if payload else None


def caller(room: dict):
    """The participant this browser is, verified against the DB, or None."""
    return storage.participant_for_uid(room, caller_uid(room["code"]))


def require_caller(room: dict) -> tuple:
    uid = caller_uid(room["code"])
    me = storage.participant_for_uid(room, uid)
    if not me:
        raise RoomError("NOT_MEMBER", "This device isn't part of this room. Rejoin from the room link.", status=403)
    return me, uid


def require_host(room: dict) -> str:
    me, uid = require_caller(room)
    if not me["is_host"]:
        raise RoomError("NOT_HOST", "Only the host can do that.", status=403)
    return uid


def redirect_for(room: dict, me: dict):
    if room["finalized"]:
        return redirect(url_for("results.results_page", room_code=room["code"]))
    if me["submitted"]:
        return redirect(url_for("picks.waiting_room", room_code=room["code"], user_name=me["name"]))
    return redirect(url_for("picks.select_items", room_code=room["code"], user_name=me["name"]))


def not_linked_page(room: dict):
    return render_template(
        "message.html",
        title="Rejoin this room",
        message="This device isn't linked to that name. Join with your name; if it says the name is taken, ask the host to tap Manage next to it and choose 'Move them to a new phone'.",
        link=url_for("join.join_room", room_code=room["code"]),
        link_text="Join room",
    ), 403
