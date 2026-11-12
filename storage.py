"""
Postgres-backed room storage.

Every multi-step write runs in one transaction that first takes the room row
lock (`SELECT ... FOR UPDATE`). That serializes writers *per room* (a handful
of people, millisecond transactions) and makes races such as finalize-vs-revoke
or two joins for the last seat resolve to exactly one outcome.

Expiry: a room is alive while last_activity_at is within ROOM_TTL_SECONDS.
Reads filter on it, so expiry is exact even though the cleanup cron is daily.

All SQL is parameterized. Never build SQL with f-strings or %-formatting.
"""
import os
import secrets
from typing import Any, Dict, Iterable, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb

import db
from utils.room_assembler import assemble_room
from utils.session import hash_secret, issue_recovery_token, issue_user_id, secrets_match
from utils.validation import name_key

# Unambiguous alphabet: no 0/O, 1/I/L.
ROOM_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
ROOM_CODE_LENGTH = 6


class RoomError(Exception):
    """A business-rule rejection. `code` is machine-readable, `status` an HTTP status."""

    def __init__(self, code: str, message: str, status: int = 409, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra


def ttl_seconds() -> int:
    try:
        return max(3600, int(os.getenv("ROOM_TTL_SECONDS", "86400")))
    except ValueError:
        return 86400


def generate_room_code() -> str:
    return "".join(secrets.choice(ROOM_CODE_ALPHABET) for _ in range(ROOM_CODE_LENGTH))


def normalize_code(code: str) -> str:
    return (code or "").strip().upper()


_ALIVE = "last_activity_at > now() - make_interval(secs => %s)"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _lock_room(cur, code: str) -> Dict[str, Any]:
    cur.execute(
        f"SELECT * FROM rooms WHERE code = %s AND {_ALIVE} FOR UPDATE",
        (code, ttl_seconds()),
    )
    row = cur.fetchone()
    if not row:
        raise RoomError("NOT_FOUND", "This room doesn't exist or has expired.", status=404)
    return row


def _require_open(room_row: Dict[str, Any]) -> None:
    if room_row["finalized_at"] is not None:
        raise RoomError("FINALIZED", "The host has already finalized this bill.", status=409)


def _touch(cur, code: str) -> None:
    cur.execute("UPDATE rooms SET last_activity_at = now() WHERE code = %s", (code,))


def _participant_by_uid(cur, code: str, uid: str) -> Optional[Dict[str, Any]]:
    if not uid:
        return None
    cur.execute(
        "SELECT * FROM participants WHERE room_code = %s AND user_id_hash = %s",
        (code, hash_secret(uid)),
    )
    return cur.fetchone()


def _participant_by_name(cur, code: str, name: str) -> Optional[Dict[str, Any]]:
    cur.execute(
        "SELECT * FROM participants WHERE room_code = %s AND name_key = %s",
        (code, name_key(name)),
    )
    return cur.fetchone()


def _insert_items(cur, code: str, items: Iterable[dict], start: int = 0) -> None:
    rows = [(code, start + idx, item["name"], item["price"]) for idx, item in enumerate(items)]
    if rows:
        cur.executemany(
            "INSERT INTO items (room_code, position, name, price) VALUES (%s, %s, %s, %s)",
            rows,
        )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_room(code: str) -> Optional[Dict[str, Any]]:
    """Assembled room dict, or None if missing/expired."""
    code = normalize_code(code)
    conn = db.get_conn()
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM rooms WHERE code = %s AND {_ALIVE}", (code, ttl_seconds()))
        room_row = cur.fetchone()
        if not room_row:
            return None
        cur.execute("SELECT * FROM participants WHERE room_code = %s", (code,))
        participants = cur.fetchall()
        cur.execute("SELECT * FROM items WHERE room_code = %s", (code,))
        items = cur.fetchall()
        cur.execute(
            "SELECT c.participant_id, c.item_id FROM claims c"
            " JOIN participants p ON p.id = c.participant_id WHERE p.room_code = %s",
            (code,),
        )
        claims = cur.fetchall()
    room = assemble_room(room_row, participants, items, claims)
    room["_device_index"] = {
        p["user_id_hash"]: p["id"] for p in participants if p["user_id_hash"]
    }
    room["_host_recovery_hash"] = room_row["host_recovery_hash"]
    return room


def room_exists(code: str) -> bool:
    conn = db.get_conn()
    row = conn.execute(
        f"SELECT 1 FROM rooms WHERE code = %s AND {_ALIVE}", (normalize_code(code), ttl_seconds())
    ).fetchone()
    return row is not None


def participant_for_uid(room: Dict[str, Any], uid: Optional[str]) -> Optional[Dict[str, Any]]:
    """Resolve a cookie uid against an already-loaded room dict (no extra query)."""
    if not uid:
        return None
    pid = room.get("_device_index", {}).get(hash_secret(uid))
    if pid is None:
        return None
    return next((p for p in room["participants"] if p["id"] == pid), None)


# ---------------------------------------------------------------------------
# Room lifecycle
# ---------------------------------------------------------------------------

def create_room(
    room_name: str,
    host_name: str,
    num_people: int,
    items: List[dict],
    ocr_data: Dict[str, Any],
    ocr_source: str,
) -> Tuple[str, str, str]:
    """Create room + host + items atomically. Returns (code, host_uid, recovery_token)."""
    conn = db.get_conn()
    host_uid = issue_user_id()
    recovery = issue_recovery_token()
    for _attempt in range(5):
        code = generate_room_code()
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO rooms (code, room_name, num_people, host_recovery_hash, ocr_source, ocr_data)"
                        " VALUES (%s, %s, %s, %s, %s, %s)",
                        (code, room_name, num_people, hash_secret(recovery), ocr_source, Jsonb(ocr_data)),
                    )
                    cur.execute(
                        "INSERT INTO participants (room_code, name, name_key, user_id_hash, is_host)"
                        " VALUES (%s, %s, %s, %s, true)",
                        (code, host_name, name_key(host_name), hash_secret(host_uid)),
                    )
                    _insert_items(cur, code, items)
            return code, host_uid, recovery
        except psycopg.errors.UniqueViolation as exc:
            if "rooms_pkey" not in str(exc):
                raise
            continue
    raise RoomError("CODE_EXHAUSTED", "Could not allocate a room code. Try again.", status=503)


def replace_items(code: str, items: List[dict], totals: Dict[str, Any]) -> bool:
    """
    Diff the host's edited item list against the DB by id: update in place,
    insert new rows, delete removed rows (their claims cascade). If anything
    changed and anyone had submitted, every submission is reset so people
    reconfirm against the new prices (their picks stay as a draft).
    Returns True if submissions were reset.
    """
    code = normalize_code(code)
    conn = db.get_conn()
    with conn.transaction(), conn.cursor() as cur:
        room_row = _lock_room(cur, code)
        _require_open(room_row)
        cur.execute("SELECT id, position, name, price FROM items WHERE room_code = %s", (code,))
        existing = {row["id"]: row for row in cur.fetchall()}

        before = sorted((r["position"], r["name"], r["price"]) for r in existing.values())
        kept_ids = set()
        for position, item in enumerate(items):
            item_id = item.get("id")
            if item_id in existing and item_id not in kept_ids:
                kept_ids.add(item_id)
                cur.execute(
                    "UPDATE items SET position = %s, name = %s, price = %s WHERE id = %s",
                    (position, item["name"], item["price"], item_id),
                )
            else:
                cur.execute(
                    "INSERT INTO items (room_code, position, name, price) VALUES (%s, %s, %s, %s)",
                    (code, position, item["name"], item["price"]),
                )
        removed = [item_id for item_id in existing if item_id not in kept_ids]
        if removed:
            cur.execute("DELETE FROM items WHERE id = ANY(%s)", (removed,))

        after = sorted((pos, it["name"], it["price"]) for pos, it in enumerate(items))
        new_ocr = dict(room_row["ocr_data"] or {})
        new_ocr.update(totals)
        new_ocr.pop("items", None)
        changed = before != after or new_ocr != (room_row["ocr_data"] or {})

        cur.execute("UPDATE rooms SET ocr_data = %s WHERE code = %s", (Jsonb(new_ocr), code))
        reset = False
        if changed:
            cur.execute(
                "UPDATE participants SET submitted_at = NULL, submission_id = NULL"
                " WHERE room_code = %s AND submitted_at IS NOT NULL",
                (code,),
            )
            reset = cur.rowcount > 0
        _touch(cur, code)
    return reset


def join_participant(code: str, raw_name: str) -> Tuple[str, Dict[str, Any], str]:
    """
    Returns (status, participant_row, uid) where status is JOINED or RECLAIMED
    (a name whose device the host reset). Raises RoomError FULL / NAME_TAKEN /
    FINALIZED / NOT_FOUND.
    """
    code = normalize_code(code)
    conn = db.get_conn()
    uid = issue_user_id()
    with conn.transaction(), conn.cursor() as cur:
        room_row = _lock_room(cur, code)
        existing = _participant_by_name(cur, code, raw_name)
        if not (existing and existing["user_id_hash"] is None):
            # Reclaiming a reset device is fine after finalize (it changes no math);
            # brand-new joins are not.
            _require_open(room_row)
        if existing:
            if existing["user_id_hash"] is None:
                cur.execute(
                    "UPDATE participants SET user_id_hash = %s WHERE id = %s RETURNING *",
                    (hash_secret(uid), existing["id"]),
                )
                row = cur.fetchone()
                _touch(cur, code)
                return "RECLAIMED", row, uid
            raise RoomError(
                "NAME_TAKEN",
                f"'{existing['name']}' has already joined from another device. "
                "Pick a different name, or ask the host to tap 'Reset device' next to that name.",
                status=409,
            )
        cur.execute("SELECT count(*) AS n FROM participants WHERE room_code = %s", (code,))
        if cur.fetchone()["n"] >= room_row["num_people"]:
            raise RoomError("FULL", "This room is full. Ask the host to increase the number of people.", status=409)
        cur.execute(
            "INSERT INTO participants (room_code, name, name_key, user_id_hash)"
            " VALUES (%s, %s, %s, %s) RETURNING *",
            (code, raw_name, name_key(raw_name), hash_secret(uid)),
        )
        row = cur.fetchone()
        _touch(cur, code)
        return "JOINED", row, uid


def submit_selection(code: str, uid: str, item_ids: List[int], submission_id: str) -> Dict[str, Any]:
    """Replace the caller's claims and mark them submitted. Idempotent on submission_id."""
    code = normalize_code(code)
    conn = db.get_conn()
    with conn.transaction(), conn.cursor() as cur:
        room_row = _lock_room(cur, code)
        _require_open(room_row)
        me = _participant_by_uid(cur, code, uid)
        if not me:
            raise RoomError("NOT_MEMBER", "You are no longer in this room.", status=403)
        if submission_id and me["submitted_at"] is not None and me["submission_id"] == submission_id:
            return {"idempotent": True, "participant": me}

        cur.execute("SELECT id FROM items WHERE room_code = %s AND id = ANY(%s)", (code, list(item_ids)))
        valid_ids = [row["id"] for row in cur.fetchall()]
        cur.execute("DELETE FROM claims WHERE participant_id = %s", (me["id"],))
        if valid_ids:
            cur.executemany(
                "INSERT INTO claims (participant_id, item_id) VALUES (%s, %s)",
                [(me["id"], item_id) for item_id in valid_ids],
            )
        cur.execute(
            "UPDATE participants SET submitted_at = now(), submission_id = %s WHERE id = %s",
            (submission_id or None, me["id"]),
        )
        _touch(cur, code)
        return {"idempotent": False, "participant": me, "item_ids": valid_ids}


def _unsubmit(cur, participant_id: int) -> None:
    cur.execute(
        "UPDATE participants SET submitted_at = NULL, submission_id = NULL WHERE id = %s",
        (participant_id,),
    )


def revoke_own(code: str, uid: str) -> Dict[str, Any]:
    code = normalize_code(code)
    conn = db.get_conn()
    with conn.transaction(), conn.cursor() as cur:
        room_row = _lock_room(cur, code)
        _require_open(room_row)
        me = _participant_by_uid(cur, code, uid)
        if not me:
            raise RoomError("NOT_MEMBER", "You are no longer in this room.", status=403)
        _unsubmit(cur, me["id"])
        _touch(cur, code)
        return me


def _require_host(cur, code: str, uid: str) -> Dict[str, Any]:
    me = _participant_by_uid(cur, code, uid)
    if not me or not me["is_host"]:
        raise RoomError("NOT_HOST", "Only the host can do that.", status=403)
    return me


def host_revoke(code: str, uid: str, target_name: str) -> None:
    code = normalize_code(code)
    conn = db.get_conn()
    with conn.transaction(), conn.cursor() as cur:
        room_row = _lock_room(cur, code)
        _require_host(cur, code, uid)
        _require_open(room_row)
        target = _participant_by_name(cur, code, target_name)
        if not target:
            raise RoomError("NO_SUCH_USER", "That person is not in this room.", status=404)
        _unsubmit(cur, target["id"])
        _touch(cur, code)


def remove_participant(code: str, uid: str, target_name: str) -> str:
    code = normalize_code(code)
    conn = db.get_conn()
    with conn.transaction(), conn.cursor() as cur:
        room_row = _lock_room(cur, code)
        _require_host(cur, code, uid)
        _require_open(room_row)
        target = _participant_by_name(cur, code, target_name)
        if not target:
            raise RoomError("NO_SUCH_USER", "That person is not in this room.", status=404)
        if target["is_host"]:
            raise RoomError("CANNOT_REMOVE_HOST", "The host can't be removed.", status=400)
        cur.execute("DELETE FROM participants WHERE id = %s", (target["id"],))
        _touch(cur, code)
        return target["name"]


def reset_device(code: str, uid: str, target_name: str) -> str:
    """Forget a guest's device so they can rejoin with the same name (keeps their picks)."""
    code = normalize_code(code)
    conn = db.get_conn()
    with conn.transaction(), conn.cursor() as cur:
        _lock_room(cur, code)
        _require_host(cur, code, uid)
        target = _participant_by_name(cur, code, target_name)
        if not target:
            raise RoomError("NO_SUCH_USER", "That person is not in this room.", status=404)
        if target["is_host"]:
            raise RoomError(
                "USE_HOST_LINK", "Use your host recovery link to move host access to another device.", status=400
            )
        cur.execute("UPDATE participants SET user_id_hash = NULL WHERE id = %s", (target["id"],))
        _touch(cur, code)
        return target["name"]


def set_num_people(code: str, uid: str, value: int) -> int:
    code = normalize_code(code)
    conn = db.get_conn()
    with conn.transaction(), conn.cursor() as cur:
        room_row = _lock_room(cur, code)
        _require_host(cur, code, uid)
        _require_open(room_row)
        cur.execute("SELECT count(*) AS n FROM participants WHERE room_code = %s", (code,))
        joined = cur.fetchone()["n"]
        if value < joined:
            raise RoomError(
                "BELOW_JOINED",
                f"Cannot set below the number of people already joined ({joined}). Remove someone first.",
                status=400,
            )
        cur.execute("UPDATE rooms SET num_people = %s WHERE code = %s", (value, code))
        _touch(cur, code)
        return value


def reopen_all(code: str, uid: str) -> None:
    """Un-finalize and send everyone back to picking (claims kept as drafts)."""
    code = normalize_code(code)
    conn = db.get_conn()
    with conn.transaction(), conn.cursor() as cur:
        _lock_room(cur, code)
        _require_host(cur, code, uid)
        cur.execute("UPDATE rooms SET finalized_at = NULL WHERE code = %s", (code,))
        cur.execute(
            "UPDATE participants SET submitted_at = NULL, submission_id = NULL WHERE room_code = %s",
            (code,),
        )
        _touch(cur, code)


def finalize_room(code: str, uid: str) -> Dict[str, Any]:
    code = normalize_code(code)
    conn = db.get_conn()
    with conn.transaction(), conn.cursor() as cur:
        room_row = _lock_room(cur, code)
        _require_host(cur, code, uid)
        if room_row["finalized_at"] is not None:
            return {"already": True}
        cur.execute(
            "SELECT name, submitted_at FROM participants WHERE room_code = %s ORDER BY joined_at, id",
            (code,),
        )
        rows = cur.fetchall()
        missing = room_row["num_people"] - len(rows)
        if missing > 0:
            raise RoomError(
                "WAITING_FOR_JOINS",
                f"Waiting for {missing} more {'person' if missing == 1 else 'people'} to join. "
                "If they aren't coming, lower the number of people.",
                status=409,
                waiting_for=missing,
            )
        editing = [r["name"] for r in rows if r["submitted_at"] is None]
        if editing:
            raise RoomError(
                "STILL_EDITING",
                f"{', '.join(editing)} {'is' if len(editing) == 1 else 'are'} still picking items.",
                status=409,
                still_editing=editing,
            )
        cur.execute("UPDATE rooms SET finalized_at = now() WHERE code = %s", (code,))
        _touch(cur, code)
        return {"already": False}


def recover_host(code: str, token: str) -> str:
    """Verify a host recovery token and bind host rights to a fresh device id."""
    code = normalize_code(code)
    conn = db.get_conn()
    with conn.transaction(), conn.cursor() as cur:
        room_row = _lock_room(cur, code)
        if not secrets_match(token or "", room_row["host_recovery_hash"]):
            raise RoomError("BAD_TOKEN", "That host link is not valid for this room.", status=403)
        uid = issue_user_id()
        cur.execute(
            "UPDATE participants SET user_id_hash = %s WHERE room_code = %s AND is_host RETURNING name",
            (hash_secret(uid), code),
        )
        row = cur.fetchone()
        _touch(cur, code)
        return uid if row else ""


# ---------------------------------------------------------------------------
# Maintenance + rate limiting
# ---------------------------------------------------------------------------

def delete_expired() -> Dict[str, int]:
    conn = db.get_conn()
    with conn.transaction(), conn.cursor() as cur:
        # Heartbeat write: keeps a free Supabase project from being paused for
        # inactivity, and records when the cron last ran.
        cur.execute(
            "INSERT INTO app_heartbeat (id, last_run, run_count) VALUES (1, now(), 1)"
            " ON CONFLICT (id) DO UPDATE SET last_run = now(), run_count = app_heartbeat.run_count + 1"
            " RETURNING last_run, run_count"
        )
        beat = cur.fetchone()
        cur.execute(
            "DELETE FROM rooms WHERE last_activity_at <= now() - make_interval(secs => %s)",
            (ttl_seconds(),),
        )
        rooms = cur.rowcount
        cur.execute("DELETE FROM rate_limits WHERE window_start < now() - interval '2 days'")
        limits = cur.rowcount
    return {
        "rooms_deleted": rooms,
        "rate_limit_rows_deleted": limits,
        "heartbeat_at": beat["last_run"].isoformat(),
        "heartbeat_count": beat["run_count"],
    }


def rate_limit_hit(bucket: str, limit: int, window_seconds: int) -> bool:
    """Count one hit in a fixed window. Returns True if the caller is still within the limit."""
    conn = db.get_conn()
    row = conn.execute(
        "INSERT INTO rate_limits (bucket, window_start, count)"
        " VALUES (%s, to_timestamp(floor(extract(epoch FROM now()) / %s) * %s), 1)"
        " ON CONFLICT (bucket, window_start) DO UPDATE SET count = rate_limits.count + 1"
        " RETURNING count",
        (bucket, window_seconds, window_seconds),
    ).fetchone()
    return row["count"] <= limit


def rate_limit_peek(bucket: str, limit: int, window_seconds: int) -> bool:
    """True if the bucket is currently under its limit (does not count a hit)."""
    conn = db.get_conn()
    row = conn.execute(
        "SELECT count FROM rate_limits WHERE bucket = %s"
        " AND window_start = to_timestamp(floor(extract(epoch FROM now()) / %s) * %s)",
        (bucket, window_seconds, window_seconds),
    ).fetchone()
    return row is None or row["count"] < limit
