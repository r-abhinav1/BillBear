"""Daily cron (vercel.json): a heartbeat write so the free Supabase project is never
paused for inactivity, then delete expired rooms and old rate limits."""
import os

from flask import Blueprint, jsonify, request

from billbear import storage
from billbear.web.observability import cron_monitor, distribution, flush

bp = Blueprint("cron", __name__)


@bp.route("/api/cron/cleanup")
def cron_cleanup():
    import hmac

    secret = os.getenv("CRON_SECRET", "").strip()
    if not secret:
        return jsonify({"status": "error", "message": "CRON_SECRET not configured"}), 503
    provided = request.headers.get("Authorization", "")
    if not hmac.compare_digest(provided.encode(), f"Bearer {secret}".encode()):
        return jsonify({"status": "error", "message": "unauthorized"}), 401
    # Reported to Sentry as a cron monitor, which emails when a day is missed or the job fails.
    with cron_monitor():
        result = storage.delete_expired()
    print(f"[cron] cleanup {result}")
    distribution("cron.rooms_deleted", result["rooms_deleted"])
    flush()
    return jsonify({"status": "success", **result})
