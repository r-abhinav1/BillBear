import json
import os
from typing import Optional

import redis

# ---------------------------------------------------------------------------
# Redis connection (private — use the CRUD helpers below, not this directly)
# ---------------------------------------------------------------------------

def get_redis_connection():
    """Return a Redis client, or None if Redis is not configured/reachable."""
    redis_url = os.getenv("REDIS_URL", "").strip()
    redis_host = os.getenv("REDIS_HOST", "").strip()

    if not redis_url and not redis_host:
        print("ℹ️ Redis not configured. Using in-memory storage for local development.")
        return None

    try:
        if redis_url:
            client = redis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
            endpoint = redis_url
        else:
            redis_port = int(os.getenv("REDIS_PORT", 6379))
            client = redis.Redis(
                host=redis_host,
                port=redis_port,
                password=os.getenv("REDIS_PASSWORD", None),
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
            endpoint = f"{redis_host}:{redis_port}"

        client.ping()
        print(f"✅ Redis connected: {endpoint}")
        return client
    except Exception as exc:
        print(f"❌ Redis connection failed: {exc}")
        print("Falling back to in-memory storage (not recommended for production)")
        return None


_redis_client = get_redis_connection()

# Always-initialised fallback dict — used when Redis is unavailable.
# Production deployments on Vercel should always configure REDIS_URL.
_fallback_rooms: dict = {}

if _redis_client is None and os.getenv("VERCEL"):
    print("⚠️  WARNING: Running on Vercel without Redis. Room data will not persist across requests.")


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------

def _room_ttl_seconds() -> int:
    try:
        return max(3600, int(os.getenv("ROOM_TTL_SECONDS", "86400")))
    except ValueError:
        return 86400


# ---------------------------------------------------------------------------
# Room CRUD
# ---------------------------------------------------------------------------

def save_room(room_code: str, room_data: dict) -> bool:
    """Persist room data to Redis or the in-memory fallback."""
    if _redis_client:
        try:
            data = room_data.copy()
            if isinstance(data.get("submitted_users"), set):
                data["submitted_users"] = list(data["submitted_users"])
            _redis_client.set(f"room:{room_code}", json.dumps(data), ex=_room_ttl_seconds())
            return True
        except Exception as exc:
            print(f"Redis save error: {exc}")
            return False

    _fallback_rooms[room_code] = room_data
    return True


def get_room(room_code: str) -> Optional[dict]:
    """Retrieve room data, or None if it doesn't exist."""
    if _redis_client:
        try:
            raw = _redis_client.get(f"room:{room_code}")
            if raw:
                data = json.loads(raw)
                if isinstance(data.get("submitted_users"), list):
                    data["submitted_users"] = set(data["submitted_users"])
                return data
            return None
        except Exception as exc:
            print(f"Redis get error: {exc}")
            return None

    return _fallback_rooms.get(room_code)


def room_exists(room_code: str) -> bool:
    """Return True if the room is present in storage."""
    if _redis_client:
        try:
            return bool(_redis_client.exists(f"room:{room_code}"))
        except Exception as exc:
            print(f"Redis exists error: {exc}")
            return False

    return room_code in _fallback_rooms


def delete_room(room_code: str) -> bool:
    """Remove a room from storage. Returns True on success."""
    if _redis_client:
        try:
            return bool(_redis_client.delete(f"room:{room_code}"))
        except Exception as exc:
            print(f"Redis delete error: {exc}")
            return False

    return _fallback_rooms.pop(room_code, None) is not None
