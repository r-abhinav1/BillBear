"""
Cookie-based session identity for room participants.

A signed cookie `bb_session_<room_code>` holds a small JSON blob identifying
the user inside a specific room. This lets a user return to the same link
(new tab, phone restart, flaky network) and be recognized as themselves
instead of being forced to pick a new name.

The cookie is room-scoped on purpose — one browser can participate in
multiple rooms at once (separate cookies), and leaving one room doesn't
affect the others.
"""

import json
import os
import secrets
import time
from typing import Optional

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer


def _secret_key() -> str:
    return os.getenv("SECRET_KEY", "dev-secret-change-in-production")


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_secret_key(), salt="bb-session-v1")


def _cookie_name(room_code: str) -> str:
    return f"bb_session_{room_code.upper()}"


def _cookie_max_age() -> int:
    try:
        return max(3600, int(os.getenv("ROOM_TTL_SECONDS", "86400")))
    except ValueError:
        return 86400


def issue_user_id() -> str:
    """Generate a short, URL-safe random ID for a new room participant."""
    return secrets.token_urlsafe(12)


def make_session_token(room_code: str, user_id: str, user_name: str) -> str:
    """Serialize {rc, uid, name, iat} into a signed, URL-safe token."""
    payload = {
        "rc": room_code.upper(),
        "uid": user_id,
        "name": user_name,
        "iat": int(time.time()),
    }
    return _serializer().dumps(payload)


def read_session_cookie(request, room_code: str) -> Optional[dict]:
    """
    Return the session payload for `room_code` if the cookie is present and
    valid, else None. Returns None silently on any failure — the caller
    treats it as "not logged in for this room".
    """
    token = request.cookies.get(_cookie_name(room_code))
    if not token:
        return None
    try:
        payload = _serializer().loads(token, max_age=_cookie_max_age())
    except SignatureExpired:
        return None
    except BadSignature:
        return None
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("rc", "").upper() != room_code.upper():
        return None
    if not payload.get("uid") or not payload.get("name"):
        return None
    return payload


def set_session_cookie(response, room_code: str, user_id: str, user_name: str):
    """Attach a signed session cookie to `response`."""
    token = make_session_token(room_code, user_id, user_name)
    response.set_cookie(
        _cookie_name(room_code),
        token,
        max_age=_cookie_max_age(),
        httponly=True,
        samesite="Lax",
        secure=bool(os.getenv("VERCEL")),
        path="/",
    )
    return response


def clear_session_cookie(response, room_code: str):
    response.delete_cookie(_cookie_name(room_code), path="/")
    return response
