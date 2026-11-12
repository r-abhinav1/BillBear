"""
Account-free identity.

* The room code/link is the invitation: anyone holding it may join.
* Joining mints a random device id (`uid`). The DB stores only sha256(uid);
  the browser gets it in a signed, HttpOnly, SameSite=Lax cookie scoped to
  that room (`bb_session_<CODE>`). Every write checks cookie uid -> DB row.
* The host additionally gets a recovery token (DB stores its hash) that can
  restore host rights on another device via /room/<code>/host?k=<token>.
* `/create` hands out a short-lived signed "scan ticket" cookie that the paid
  vision endpoint requires, which stops naive scripted abuse.
"""
import hashlib
import hmac
import os
import secrets
import time
from typing import Optional

from itsdangerous import BadSignature, URLSafeTimedSerializer

DEFAULT_SECRET = "dev-secret-change-in-production"
SESSION_MAX_AGE = 7 * 24 * 3600      # comfortably outlives a 24h-idle room
HOSTKEY_MAX_AGE = 7 * 24 * 3600
SCAN_TICKET_MAX_AGE = 15 * 60


def get_secret_key() -> str:
    """
    Fail closed in production: forging cookies with a known default key would
    let anyone become host of any room.
    """
    key = os.getenv("SECRET_KEY", "").strip()
    if os.getenv("VERCEL") and (not key or key == DEFAULT_SECRET or len(key) < 32):
        raise RuntimeError(
            "SECRET_KEY must be set to a random value of at least 32 characters in production "
            "(python -c \"import secrets; print(secrets.token_urlsafe(48))\")."
        )
    if not key:
        print("[session] WARNING: SECRET_KEY not set — using an insecure development key.")
        return DEFAULT_SECRET
    return key


def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_secret_key(), salt=salt)


def _secure_cookies() -> bool:
    return bool(os.getenv("VERCEL")) or os.getenv("COOKIE_SECURE", "").lower() in {"1", "true"}


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def secrets_match(value: str, expected_hash: Optional[str]) -> bool:
    if not value or not expected_hash:
        return False
    return hmac.compare_digest(hash_secret(value), expected_hash)


def issue_user_id() -> str:
    return secrets.token_urlsafe(18)


def issue_recovery_token() -> str:
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# Room session cookie
# ---------------------------------------------------------------------------

def session_cookie_name(room_code: str) -> str:
    return f"bb_session_{room_code.upper()}"


def make_session_token(room_code: str, user_id: str, user_name: str = "") -> str:
    payload = {"rc": room_code.upper(), "uid": user_id, "iat": int(time.time())}
    if user_name:
        payload["name"] = user_name
    return _serializer("bb-session-v1").dumps(payload)


def read_session_cookie(request, room_code: str) -> Optional[dict]:
    """Return the verified payload for this room, or None."""
    token = request.cookies.get(session_cookie_name(room_code))
    if not token:
        return None
    try:
        payload = _serializer("bb-session-v1").loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:  # includes SignatureExpired
        return None
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("rc", "")).upper() != room_code.upper():
        return None
    if not payload.get("uid"):
        return None
    return payload


def set_session_cookie(response, room_code: str, user_id: str, user_name: str = ""):
    response.set_cookie(
        session_cookie_name(room_code),
        make_session_token(room_code, user_id, user_name),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="Lax",
        secure=_secure_cookies(),
        path="/",
    )
    return response


def clear_session_cookie(response, room_code: str):
    response.delete_cookie(session_cookie_name(room_code), path="/")
    return response


# ---------------------------------------------------------------------------
# Host recovery link (kept in a cookie on the host's device so the page can
# keep showing "save your host link" — the DB only has the hash)
# ---------------------------------------------------------------------------

def hostkey_cookie_name(room_code: str) -> str:
    return f"bb_hostkey_{room_code.upper()}"


def set_hostkey_cookie(response, room_code: str, token: str):
    response.set_cookie(
        hostkey_cookie_name(room_code),
        _serializer("bb-hostkey-v1").dumps({"rc": room_code.upper(), "k": token}),
        max_age=HOSTKEY_MAX_AGE,
        httponly=True,
        samesite="Lax",
        secure=_secure_cookies(),
        path="/",
    )
    return response


def read_hostkey_cookie(request, room_code: str) -> Optional[str]:
    raw = request.cookies.get(hostkey_cookie_name(room_code))
    if not raw:
        return None
    try:
        payload = _serializer("bb-hostkey-v1").loads(raw, max_age=HOSTKEY_MAX_AGE)
    except Exception:
        return None
    if not isinstance(payload, dict) or str(payload.get("rc", "")).upper() != room_code.upper():
        return None
    return payload.get("k") or None


# ---------------------------------------------------------------------------
# Scan ticket
# ---------------------------------------------------------------------------

SCAN_TICKET_COOKIE = "bb_scan_ticket"


def set_scan_ticket(response):
    response.set_cookie(
        SCAN_TICKET_COOKIE,
        _serializer("bb-scan-v1").dumps({"n": secrets.token_urlsafe(8)}),
        max_age=SCAN_TICKET_MAX_AGE,
        httponly=True,
        samesite="Strict",
        secure=_secure_cookies(),
        path="/api/receipt",
    )
    return response


def has_valid_scan_ticket(request) -> bool:
    raw = request.cookies.get(SCAN_TICKET_COOKIE)
    if not raw:
        return False
    try:
        _serializer("bb-scan-v1").loads(raw, max_age=SCAN_TICKET_MAX_AGE)
        return True
    except Exception:
        return False
