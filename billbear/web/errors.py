"""Error pages and JSON errors, one shape for every route."""
from flask import current_app, jsonify, render_template, request, url_for
from werkzeug.exceptions import RequestEntityTooLarge

from billbear import db
from billbear.storage import RoomError
from billbear.web.observability import capture


def wants_json() -> bool:
    if (
        request.headers.get("X-Requested-With") == "fetch"
        or request.accept_mimetypes.best == "application/json"
        or request.path.startswith("/api/")
        or request.is_json
    ):
        return True
    try:
        return request.form.get("ajax") == "1"
    except Exception:  # e.g. body too large — don't re-raise while building an error page
        return False


def error_response(message: str, status: int, code: str = "ERROR", **extra):
    if wants_json():
        body = {"status": "error", "code": code, "message": message}
        body.update(extra)
        return jsonify(body), status
    title = ERROR_TITLES.get(code, "Something went wrong")
    # Errors about a room say what to do next, when the URL names the room.
    in_room = (request.view_args or {}).get("room_code")
    link = link_text = None
    if in_room and code == "NOT_HOST":
        link, link_text = url_for("rooms.room_summary", room_code=in_room), "Back to the room"
    elif in_room and code == "NOT_MEMBER":
        link, link_text = url_for("join.join_room", room_code=in_room), "Join the room"
    return render_template("message.html", title=title, message=message, link=link, link_text=link_text), status


# Page titles for the errors people actually meet. Anything else is "Something went wrong".
ERROR_TITLES = {
    "NOT_HOST": "Only the host can do that",
    "NOT_MEMBER": "Rejoin this room",
    "FULL": "This room is full",
    "FINALIZED": "The bill is already split",
    "RATE_LIMITED": "Too many tries",
}


def _room_error(exc: RoomError):
    if exc.code == "NOT_FOUND" and not wants_json():
        return render_template(
            "message.html",
            title="Bear ate this room",
            message="This room doesn't exist or has expired. Rooms are cleared 24 hours after the last activity, so check the code with your host or start a new split.",
        ), 404
    return error_response(exc.message, exc.status, exc.code, **exc.extra)


def _db_down(exc):
    capture(exc, kind="db_down")
    return error_response("The database is temporarily unavailable. Please try again in a moment.", 503, "DB_DOWN")


def _too_large(exc):
    return error_response("Upload is too large (max 4 MB).", 413, "TOO_LARGE")


def _unhandled(exc):
    from werkzeug.exceptions import HTTPException

    if isinstance(exc, HTTPException):
        return exc
    current_app.logger.exception("Unhandled error")
    capture(exc, kind="unhandled")
    return error_response("Something went wrong on our side. Please try again.", 500, "SERVER_ERROR")


def register_error_handlers(app):
    app.register_error_handler(RoomError, _room_error)
    app.register_error_handler(db.DatabaseUnavailable, _db_down)
    app.register_error_handler(RequestEntityTooLarge, _too_large)
    app.register_error_handler(Exception, _unhandled)
