import base64
import json
import os
import re
import sys
from datetime import datetime
from io import BytesIO
from itertools import zip_longest

import qrcode
from dotenv import load_dotenv
from flask import Flask, jsonify, make_response, redirect, render_template, request, url_for
from werkzeug.exceptions import RequestEntityTooLarge

load_dotenv()

# Write log lines immediately even when stdout is redirected to a file.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

import db  # noqa: E402
import storage  # noqa: E402
from storage import RoomError  # noqa: E402
from utils.bill_audit import audit_bill  # noqa: E402
from utils.bill_split import calculate_bill_split  # noqa: E402
from utils.image_prep import ImageRejected, prepare_receipt_image  # noqa: E402
from utils.receipt_contracts import (  # noqa: E402
    AMOUNT_FIELDS,
    normalize_item_category,
    normalize_receipt_payload,
)
from utils.receipt_pipeline import VisionFailed, VisionUnavailable, extract_receipt_from_vision  # noqa: E402
from utils.security import (  # noqa: E402
    RATE_LIMITS,
    apply_security_headers,
    client_ip,
    is_same_origin,
    scan_daily_cap,
    turnstile_enabled,
    turnstile_site_key,
    verify_turnstile,
)
from utils.session import (  # noqa: E402
    get_secret_key,
    has_valid_scan_ticket,
    read_hostkey_cookie,
    read_session_cookie,
    set_hostkey_cookie,
    set_scan_ticket,
    set_session_cookie,
)
from utils.validation import (  # noqa: E402
    clean_items,
    normalize_quantity,
    format_price,
    normalize_person_name,
    normalize_room_name,
    parse_price,
)

VERCEL_ENV = bool(os.getenv("VERCEL"))

# ---------------------------------------------------------------------------
# PDF library detection
# ---------------------------------------------------------------------------

PDF_AVAILABLE = False
WEASYPRINT_AVAILABLE = False
XHTML2PDF_AVAILABLE = False

if not VERCEL_ENV:
    try:
        import weasyprint
        WEASYPRINT_AVAILABLE = True
        PDF_AVAILABLE = True
    except Exception as exc:
        print(f"WeasyPrint not available, trying xhtml2pdf... ({exc})")

if not PDF_AVAILABLE:
    try:
        from xhtml2pdf import pisa
        XHTML2PDF_AVAILABLE = True
        PDF_AVAILABLE = True
    except Exception as exc:
        print(f"xhtml2pdf not available ({exc})")

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)
if VERCEL_ENV or os.getenv("TRUST_PROXY", "").lower() in {"1", "true", "yes"}:
    # Behind Vercel / ngrok, TLS ends at the proxy. Trust its X-Forwarded-Proto/Host
    # so request.host_url (same-origin check, QR links) is the public https URL.
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config["SECRET_KEY"] = get_secret_key()  # raises on Vercel if missing/weak
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
db.init_app(app)

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


# ---------------------------------------------------------------------------
# Request plumbing: CSRF origin check, headers, errors
# ---------------------------------------------------------------------------

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
    return render_template("message.html", title="Something went wrong", message=message), status


@app.before_request
def _csrf_origin_check():
    if request.method in UNSAFE_METHODS and not is_same_origin(request):
        return error_response("Cross-site request blocked.", 403, "BAD_ORIGIN")
    return None


@app.after_request
def _security_headers(response):
    return apply_security_headers(response, turnstile=turnstile_enabled())


@app.errorhandler(RoomError)
def _room_error(exc: RoomError):
    if exc.code == "NOT_FOUND" and not wants_json():
        return render_template(
            "message.html",
            title="Bear ate this room",
            message="This room doesn't exist or has expired. Rooms are cleared 24 hours after the last activity, so check the code with your host or start a new split.",
        ), 404
    return error_response(exc.message, exc.status, exc.code, **exc.extra)


@app.errorhandler(db.DatabaseUnavailable)
def _db_down(exc):
    return error_response("The database is temporarily unavailable. Please try again in a moment.", 503, "DB_DOWN")


@app.errorhandler(RequestEntityTooLarge)
def _too_large(exc):
    return error_response("Upload is too large (max 4 MB).", 413, "TOO_LARGE")


@app.errorhandler(Exception)
def _unhandled(exc):
    from werkzeug.exceptions import HTTPException

    if isinstance(exc, HTTPException):
        return exc
    app.logger.exception("Unhandled error")
    return error_response("Something went wrong on our side. Please try again.", 500, "SERVER_ERROR")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def rate_limited(name: str) -> bool:
    """Count a hit; True if the caller is over the limit."""
    limit, window = RATE_LIMITS[name]
    return not storage.rate_limit_hit(f"{name}:{client_ip(request)}", limit, window)


def _miss_blocked() -> bool:
    limit, window = RATE_LIMITS["miss"]
    return not storage.rate_limit_peek(f"miss:{client_ip(request)}", limit, window)


def load_room(code: str) -> dict:
    """Load an alive room or raise. Repeated misses from one IP get a 429 (code guessing)."""
    code = storage.normalize_code(code)
    if _miss_blocked():
        raise RoomError("RATE_LIMITED", "Too many attempts. Wait a few minutes and try again.", status=429)
    room = storage.get_room(code) if re.fullmatch(r"[A-Z0-9]{6}", code) else None
    if room is None:
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


def _lan_ip():
    """This machine's Wi-Fi/LAN address (no packets are sent)."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
    except OSError:
        return None


_NGROK_CACHE = {"url": None, "checked": 0.0}


def _ngrok_public_url():
    """
    Local dev only: the https URL of a running `ngrok http` tunnel, read from
    ngrok's local agent API (localhost:4040). Cached for 30s; None if no tunnel.
    """
    import time

    import requests as _requests

    now = time.monotonic()
    if now - _NGROK_CACHE["checked"] < 30:
        return _NGROK_CACHE["url"]
    url = None
    try:
        tunnels = _requests.get("http://127.0.0.1:4040/api/tunnels", timeout=0.3).json().get("tunnels", [])
        https = [t.get("public_url", "") for t in tunnels if str(t.get("public_url", "")).startswith("https://")]
        url = https[0].rstrip("/") if https else None
    except Exception:
        url = None
    _NGROK_CACHE.update(url=url, checked=now)
    return url


def share_url(endpoint: str, **values) -> str:
    """
    Absolute URL for links other people open (QR code, join link).
    Order: PUBLIC_BASE_URL if set. In local dev, when the page was opened via
    localhost: a running ngrok tunnel (works on mobile data too), else this
    machine's Wi-Fi address (same network only). Otherwise the current host.
    """
    path = url_for(endpoint, **values)
    base = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if base:
        return base + path
    host = request.host.split(":")[0]
    if not VERCEL_ENV and host in {"localhost", "127.0.0.1", "::1", "[::1]"}:
        tunnel = _ngrok_public_url()
        if tunnel:
            return tunnel + path
        lan = _lan_ip()
        if lan:
            port = request.host.split(":")[1] if ":" in request.host else ""
            return f"{request.scheme}://{lan}{':' + port if port else ''}{path}"
    return url_for(endpoint, _external=True, **values)


def generate_qr_base64(link: str) -> str:
    buf = BytesIO()
    qrcode.make(link).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def host_recovery_url(room_code: str):
    token = read_hostkey_cookie(request, room_code)
    if not token:
        return None
    return url_for("host_recover", room_code=room_code, k=token, _external=True)


def _receipt_totals(normalized: dict) -> dict:
    totals = {field: normalized.get(field, "N/A") for field in AMOUNT_FIELDS}
    for field in ("restaurant", "date", "time"):
        value = normalized.get(field)
        if isinstance(value, str) and value:
            totals[field] = value[:120]
    return totals


def _redirect_for(room: dict, me: dict):
    if room["finalized"]:
        return redirect(url_for("results_page", room_code=room["code"]))
    if me["submitted"]:
        return redirect(url_for("waiting_room", room_code=room["code"], user_name=me["name"]))
    return redirect(url_for("select_items", room_code=room["code"], user_name=me["name"]))


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 _-]+", "", name).strip().replace(" ", "_")
    return (cleaned or "bill")[:50]


# Images the PDF template may embed, referenced there as "bbasset/<name>".
_PDF_ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "images")
_PDF_ASSETS = {
    "pdf-bear.png", "pdf-watermark.png",
    "pdf-veg.png", "pdf-nonveg.png", "pdf-egg.png", "pdf-alcohol.png", "pdf-blank.png",
}


def _pdf_link_callback(uri: str, rel: str) -> str:
    """Resolve our own bundled images; block every other resource the HTML names."""
    prefix = "bbasset/"
    if uri.startswith(prefix) and uri[len(prefix):] in _PDF_ASSETS:
        return os.path.join(_PDF_ASSET_DIR, uri[len(prefix):])
    return ""


def generate_pdf_response(room: dict, room_code: str):
    bill_split = calculate_bill_split(room)
    all_square = all(w.get("severity") in ("ok", "note") for w in audit_bill(room, bill_split))
    html = render_template(
        "results_pdf.html",
        room=room,
        room_code=room_code,
        bill_split=bill_split,
        all_square=all_square,
        current_date=datetime.now(),
    )
    buf = BytesIO()
    filename = f"{_safe_filename(room['room_name'])}_bill_split.pdf"
    if WEASYPRINT_AVAILABLE and not VERCEL_ENV:
        from pathlib import Path

        html = html.replace("bbasset/", Path(_PDF_ASSET_DIR).as_uri() + "/")
        weasyprint.HTML(string=html).write_pdf(buf)
    elif XHTML2PDF_AVAILABLE:
        status = pisa.CreatePDF(html, dest=buf, link_callback=_pdf_link_callback)
        if status.err:
            raise RuntimeError("xhtml2pdf failed")
    else:
        raise RuntimeError("No PDF library")
    response = make_response(buf.getvalue())
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


# ---------------------------------------------------------------------------
# Static pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


# Help: an index and one page per topic, read in this order.
HELP_TOPICS = [
    {"slug": "basics", "title": "The basics", "blurb": "From scan to split in five steps"},
    {"slug": "sharing", "title": "Shared or yours", "blurb": "When the bill has several of one dish"},
    {"slug": "tax", "title": "Tax and service", "blurb": "Who pays how much of the charges"},
    {"slug": "host", "title": "For the host", "blurb": "Everything you can do in the room"},
    {"slug": "scan", "title": "The scan", "blurb": "How Bear reads the bill, and where it slips"},
]


@app.route("/help")
def help_index():
    return render_template("help/index.html", topics=HELP_TOPICS)


@app.route("/help/<topic>")
def help_topic(topic):
    index = next((i for i, t in enumerate(HELP_TOPICS) if t["slug"] == topic), None)
    if index is None:
        return redirect(url_for("help_index"))
    return render_template(
        f"help/{HELP_TOPICS[index]['slug']}.html",
        topics=HELP_TOPICS,
        topic=HELP_TOPICS[index],
        position=index + 1,
        next_topic=HELP_TOPICS[index + 1] if index + 1 < len(HELP_TOPICS) else None,
    )


@app.route("/about")
def about():
    # The About page was folded into Help.
    return redirect(url_for("help_index"), code=301)


@app.route("/terms")
def terms():
    return render_template("terms.html")


# ---------------------------------------------------------------------------
# Receipt scan (vision only). No room is touched here.
# ---------------------------------------------------------------------------

@app.route("/api/receipt/scan", methods=["POST"])
def scan_receipt():
    if not has_valid_scan_ticket(request):
        return error_response("Scan session expired. Reload the page and try again.", 403, "NO_TICKET")
    if not verify_turnstile(request.form.get("cf-turnstile-response", ""), client_ip(request)):
        return error_response("Bot check failed. Reload the page and try again.", 403, "BOT_CHECK")
    if rate_limited("scan") or rate_limited("scan_day"):
        return error_response(
            "Too many scans from this network. Wait a bit, or enter items manually.", 429, "RATE_LIMITED"
        )
    # Global budget ceiling (checked after per-IP limits so one abuser can't
    # burn everyone's daily allowance with rejected requests).
    if not storage.rate_limit_hit("scan_global:all", scan_daily_cap(), 86400):
        return error_response(
            "Bill scanning has hit today's limit. Please enter items manually — it resets tomorrow.",
            429,
            "DAILY_CAP",
        )

    upload = request.files.get("image")
    if not upload:
        return error_response("No image uploaded.", 400, "NO_IMAGE")
    try:
        image_b64, mime = prepare_receipt_image(upload.read())
    except ImageRejected as exc:
        return error_response(str(exc), 400, "BAD_IMAGE")

    try:
        receipt = extract_receipt_from_vision(image_b64, mime)
    except VisionUnavailable as exc:
        return error_response(str(exc), 503, "SCAN_UNAVAILABLE")
    except VisionFailed as exc:
        return error_response(
            f"{exc} Try a clearer, well-lit photo, or enter items manually.", 502, "SCAN_FAILED"
        )

    items = clean_items(receipt.get("items"))
    receipt["items"] = [
        {"name": i["name"], "price": format_price(i["price"]), "category": i["category"],
         "quantity": i["quantity"]}
        for i in items
    ]
    receipt.pop("parser_meta", None)
    return jsonify({"status": "success", "receipt": receipt})


# ---------------------------------------------------------------------------
# Room creation and item editing (host only)
# ---------------------------------------------------------------------------

def _render_create(mode: str, error: str = "", status: int = 200, form=None):
    response = make_response(
        render_template(
            "create_room.html",
            mode=mode,
            error=error,
            form=form or {},
            turnstile_site_key=turnstile_site_key() if turnstile_enabled() else "",
        ),
        status,
    )
    set_scan_ticket(response)
    return response


@app.route("/create", methods=["GET", "POST"])
def create_room():
    mode = (request.values.get("mode") or "image").lower()
    if mode not in {"image", "json"}:
        mode = "image"
    if request.method == "GET":
        return _render_create(mode)

    form = request.form
    if rate_limited("create"):
        return _render_create(mode, "Too many rooms created from this network. Try again later.", 429, form)
    if not verify_turnstile(form.get("cf-turnstile-response", ""), client_ip(request)):
        return _render_create(mode, "Bot check failed. Please try again.", 403, form)

    try:
        host_name = normalize_person_name(form.get("host_name"))
        room_name = normalize_room_name(form.get("room_name"))
        try:
            num_people = int(form.get("num_people", "").strip())
        except ValueError:
            raise ValueError("Number of people must be a whole number.")
        if not 1 <= num_people <= 50:
            raise ValueError("Number of people must be between 1 and 50.")

        if form.get("skip_ocr", "").lower() == "true":
            receipt, source = {}, "manual"
        else:
            raw = form.get("receipt_json", "").strip()
            if not raw:
                raise ValueError("Scan the bill first, or tap 'Enter items manually'.")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                raise ValueError("The scanned receipt data was corrupted. Please scan again.")
            if not isinstance(parsed, dict):
                raise ValueError("The scanned receipt data was invalid. Please scan again.")
            receipt = parsed.get("_vision_receipt") if isinstance(parsed.get("_vision_receipt"), dict) else parsed
            meta_source = (receipt.get("extraction_meta") or {}).get("source")
            source = meta_source if meta_source in {"vision_llm", "user_saved_json"} else "client_json"

        normalized = normalize_receipt_payload(receipt)
        items = clean_items(normalized.get("items"))
    except ValueError as exc:
        return _render_create(mode, str(exc), 400, form)

    code, host_uid, recovery_token = storage.create_room(
        room_name=room_name,
        host_name=host_name,
        num_people=num_people,
        items=items,
        ocr_data=_receipt_totals(normalized),
        ocr_source=source,
    )
    response = make_response(redirect(url_for("edit_items", room_code=code)))
    set_session_cookie(response, code, host_uid, host_name)
    set_hostkey_cookie(response, code, recovery_token)
    return response


@app.route("/edit/<room_code>", methods=["GET", "POST"])
def edit_items(room_code):
    room = load_room(room_code)
    require_host(room)
    code = room["code"]

    if room["finalized"]:
        return render_template(
            "message.html",
            title="Bill is finalized",
            message="Reopen selections from the results page before editing items.",
            link=url_for("results_page", room_code=code),
            link_text="Go to results",
        ), 409

    error = ""
    if request.method == "POST":
        ids = request.form.getlist("item_id")
        names = request.form.getlist("item_name")
        prices = request.form.getlist("item_price")
        categories = request.form.getlist("item_category")
        quantities = request.form.getlist("item_quantity")
        rows = [
            {"id": item_id, "name": name, "price": price, "category": category, "quantity": quantity}
            for item_id, name, price, category, quantity in zip_longest(ids, names, prices, categories, quantities)
            if name is not None
        ]
        try:
            items = clean_items(rows, strict_prices=True)
            totals = {}
            for field in AMOUNT_FIELDS:
                totals[field] = format_price(parse_price(request.form.get(field)))
            # The switch is a checkbox, so "off" arrives as a missing field; the
            # marker says the form had the switch at all.
            if "tax_split_set" in request.form:
                totals["taxSplit"] = "proportional" if request.form.get("tax_proportional") else "equal"
        except ValueError as exc:
            error = str(exc)
        else:
            storage.replace_items(code, items, totals)
            return redirect(url_for("room_summary", room_code=code))

    view = dict(room["ocr_data"])
    view["items"] = [
        {"id": i["id"], "name": i["raw_name"], "price": i["price"], "category": i["category"],
         "quantity": i["quantity"]}
        for i in room["items"]
    ]
    if error:
        # Re-show what the host typed so one bad price doesn't wipe their edits.
        view["items"] = [
            {
                "id": r["id"],
                "name": r["name"],
                "price": r["price"] or "",
                "category": normalize_item_category(r["category"]),
                "quantity": normalize_quantity(r["quantity"]),
            }
            for r in rows
        ]
        view.update({field: request.form.get(field, "N/A") for field in AMOUNT_FIELDS})
        if "tax_split_set" in request.form:
            view["taxSplit"] = "proportional" if request.form.get("tax_proportional") else "equal"
    return render_template(
        "edit_items.html",
        room_code=code,
        items=view,
        error=error,
        submitted_count=len(room["submitted_users"]),
    ), (400 if error else 200)


# ---------------------------------------------------------------------------
# Lobby and status
# ---------------------------------------------------------------------------

@app.route("/room/<room_code>")
def room_summary(room_code):
    room = load_room(room_code)
    me = caller(room)
    join_url = share_url("join_room", room_code=room["code"])
    return render_template(
        "room_summary.html",
        room=room,
        room_code=room["code"],
        qr_b64=generate_qr_base64(join_url),
        join_url=join_url,
        me=me,
        is_host=bool(me and me["is_host"]),
        host_link=host_recovery_url(room["code"]) if me and me["is_host"] else None,
    )


def _picks_for_host(room: dict) -> dict:
    """Each person's sent picks as display strings, e.g. "Masala Dosa ×2" or "Naan (sharing)"."""
    several = {item["name"] for item in room["items"] if item["quantity"] > 1}
    picks = {}
    for name, dishes in room["selections"].items():
        counts = room["portions"].get(name, {})
        shown = []
        for dish in dishes:
            if dish not in several:
                shown.append(dish)
            elif counts.get(dish, 0) == 0:
                shown.append(f"{dish} (sharing)")
            else:
                shown.append(f"{dish} ×{counts[dish]}")
        picks[name] = shown
    return picks


@app.route("/room/<room_code>/status")
def room_status(room_code):
    room = load_room(room_code)
    me = caller(room)
    users = room["users"]
    submitted = [u for u in users if u in room["submitted_users"]]
    return jsonify({
        "status": "success",
        "users": users,
        "submitted_users": submitted,
        "devices": {p["name"]: p["has_device"] for p in room["participants"]},
        "all_submitted": bool(users) and len(submitted) == len(users),
        "enough_users_joined": room["waiting_for"] == 0,
        "ready_to_proceed": room["can_finalize"],
        "can_finalize": room["can_finalize"],
        "finalized": room["finalized"],
        "still_editing": room["still_editing"],
        "waiting_for": room["waiting_for"],
        "unclaimed_items": room["unclaimed_items"],
        "total_users": len(users),
        "expected_people": room["num_people"],
        "submitted_count": len(submitted),
        "host_name": room["host_name"],
        "caller_name": me["name"] if me else None,
        "caller_in_room": bool(me),
        "caller_is_host": bool(me and me["is_host"]),
        # What each person has sent, for the host's Manage sheet. Guests only
        # see everyone's picks once the bill is final.
        "picks": _picks_for_host(room) if me and me["is_host"] else None,
        # Dishes where the portions sent don't add up to the bill's quantity.
        "quantity_mismatches": room["quantity_mismatches"] if me and me["is_host"] else [],
        # For dishes the bill lists several of: what each *other* person has sent
        # (units claimed, or 0 for sharing), so someone picking can see what is left.
        "taken": {
            str(item_id): [h for h in holders if h["name"] != me["name"]]
            for item_id, holders in room["taken"].items()
        } if me else {},
    })


# ---------------------------------------------------------------------------
# Joining
# ---------------------------------------------------------------------------

def _do_join(code: str, raw_name: str):
    """Shared join logic. Returns a response on success, raises ValueError/RoomError otherwise."""
    name = normalize_person_name(raw_name)
    if rate_limited("join"):
        raise RoomError("RATE_LIMITED", "Too many join attempts. Wait a few minutes.", status=429)
    status, row, uid = storage.join_participant(code, name)
    print(f"[join] {status} room={code}")
    response = make_response(redirect(url_for("user_room", room_code=code, user_name=row["name"])))
    set_session_cookie(response, code, uid, row["name"])
    return response


@app.route("/join", methods=["GET", "POST"])
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
        return _redirect_for(room, me)
    try:
        return _do_join(room["code"], request.form.get("user_name"))
    except (ValueError, RoomError) as exc:
        message = exc.message if isinstance(exc, RoomError) else str(exc)
        return render_template("join_room.html", error=message), getattr(exc, "status", 400)


@app.route("/join/<room_code>", methods=["GET", "POST"])
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
        return _redirect_for(room, me)
    if request.method == "POST":
        try:
            return _do_join(code, request.form.get("user_name"))
        except (ValueError, RoomError) as exc:
            message = exc.message if isinstance(exc, RoomError) else str(exc)
            return render_template(
                "join_room_direct.html", error=message, room=room, room_code=code
            ), getattr(exc, "status", 400)
    if room["finalized"]:
        return render_template(
            "join_room_direct.html",
            error="This bill has already been finalized. If you were part of it, ask the host to move you to a new phone (Manage, next to your name), then join with the same name.",
            room=room,
            room_code=code,
        )
    return render_template("join_room_direct.html", room=room, room_code=code)


@app.route("/room/<room_code>/host", methods=["GET", "POST"])
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
    try:
        uid = storage.recover_host(code, token)
    except RoomError as exc:
        if exc.code == "BAD_TOKEN":
            rate_limited("miss")
        raise
    response = make_response(redirect(url_for("room_summary", room_code=code)))
    set_session_cookie(response, code, uid, room["host_name"])
    set_hostkey_cookie(response, code, token)
    return response


# ---------------------------------------------------------------------------
# Item selection and waiting
# ---------------------------------------------------------------------------

def _not_linked_page(room: dict):
    return render_template(
        "message.html",
        title="Rejoin this room",
        message="This device isn't linked to that name. Join with your name; if it says the name is taken, ask the host to tap Manage next to it and choose 'Move them to a new phone'.",
        link=url_for("join_room", room_code=room["code"]),
        link_text="Join room",
    ), 403


@app.route("/room/<room_code>/user/<user_name>")
def user_room(room_code, user_name):
    room = load_room(room_code)
    me = caller(room)
    if not me:
        return _not_linked_page(room)
    return _redirect_for(room, me)


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


@app.route("/room/<room_code>/user/<user_name>/select-items", methods=["GET", "POST"])
def select_items(room_code, user_name):
    room = load_room(room_code)
    code = room["code"]

    if request.method == "POST":
        me, uid = require_caller(room)
        if me["name"] != user_name:
            raise RoomError("NOT_YOUR_SESSION", "You can only submit your own selection.", status=403)
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
        target = url_for("waiting_room", room_code=code, user_name=me["name"])
        if wants_json():
            return jsonify({"status": "success", "idempotent": result["idempotent"], "redirect": target})
        return redirect(target)

    me = caller(room)
    if not me:
        return _not_linked_page(room)
    if me["name"] != user_name or room["finalized"] or me["submitted"]:
        return _redirect_for(room, me)
    return render_template(
        "select_items.html",
        room=room,
        room_code=code,
        user_name=me["name"],
        pre_selected=me["claim_ids"],
        pre_portions=me["claim_portions"],
    )


@app.route("/api/room/<room_code>/my-status")
def my_status(room_code):
    room = load_room(room_code)
    me = caller(room)
    if not me:
        return jsonify({"status": "success", "authenticated": False, "in_room": False, "submitted": False})
    names = {i["id"]: i["name"] for i in room["items"]}
    if room["finalized"]:
        target = url_for("results_page", room_code=room["code"])
    elif me["submitted"]:
        target = url_for("waiting_room", room_code=room["code"], user_name=me["name"])
    else:
        target = url_for("select_items", room_code=room["code"], user_name=me["name"])
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


@app.route("/room/<room_code>/waiting")
@app.route("/room/<room_code>/waiting/<user_name>")
def waiting_room(room_code, user_name=None):
    room = load_room(room_code)
    me = caller(room)
    if not me:
        return _not_linked_page(room)
    if room["finalized"]:
        return redirect(url_for("results_page", room_code=room["code"]))
    if user_name != me["name"]:
        return redirect(url_for("waiting_room", room_code=room["code"], user_name=me["name"]))
    join_url = share_url("join_room", room_code=room["code"])
    return render_template(
        "waiting_room.html",
        room=room,
        room_code=room["code"],
        user_name=me["name"],
        is_host=me["is_host"],
        qr_b64=generate_qr_base64(join_url),
        join_url=join_url,
        host_link=host_recovery_url(room["code"]) if me["is_host"] else None,
    )


# ---------------------------------------------------------------------------
# Self-service revoke and host controls
# ---------------------------------------------------------------------------

def _json_body() -> dict:
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    return request.form.to_dict()


@app.route("/room/<room_code>/user/<user_name>/revoke", methods=["POST"])
def revoke_own_selection(room_code, user_name):
    room = load_room(room_code)
    me, uid = require_caller(room)
    if me["name"] != user_name:
        raise RoomError("NOT_YOUR_SESSION", "Not your session.", status=403)
    storage.revoke_own(room["code"], uid)
    return jsonify({
        "status": "success",
        "redirect": url_for("select_items", room_code=room["code"], user_name=me["name"]),
    })


@app.route("/room/<room_code>/host/remove-user", methods=["POST"])
def host_remove_user(room_code):
    room = load_room(room_code)
    uid = require_host(room)
    target = str(_json_body().get("target_user", "")).strip()
    if not target:
        raise RoomError("BAD_REQUEST", "Missing target_user", status=400)
    removed = storage.remove_participant(room["code"], uid, target)
    return jsonify({"status": "success", "removed": removed})


@app.route("/room/<room_code>/host/reset-device", methods=["POST"])
def host_reset_device(room_code):
    room = load_room(room_code)
    uid = require_host(room)
    target = str(_json_body().get("target_user", "")).strip()
    if not target:
        raise RoomError("BAD_REQUEST", "Missing target_user", status=400)
    name = storage.reset_device(room["code"], uid, target)
    return jsonify({"status": "success", "reset": name})


@app.route("/room/<room_code>/host/num-people", methods=["POST"])
def host_update_num_people(room_code):
    room = load_room(room_code)
    uid = require_host(room)
    try:
        value = int(str(_json_body().get("num_people", "")).strip())
    except (TypeError, ValueError):
        raise RoomError("BAD_REQUEST", "num_people must be an integer", status=400)
    if not 1 <= value <= 50:
        raise RoomError("BAD_REQUEST", "num_people must be between 1 and 50", status=400)
    storage.set_num_people(room["code"], uid, value)
    return jsonify({"status": "success", "num_people": value})


@app.route("/room/<room_code>/host/reopen-selections", methods=["POST"])
def host_reopen_selections(room_code):
    room = load_room(room_code)
    uid = require_host(room)
    storage.reopen_all(room["code"], uid)
    return jsonify({"status": "success"})


@app.route("/room/<room_code>/host/revoke/<target_user>", methods=["POST"])
def host_revoke_user(room_code, target_user):
    room = load_room(room_code)
    uid = require_host(room)
    storage.host_revoke(room["code"], uid, target_user)
    return jsonify({"status": "success", "revoked": target_user})


@app.route("/room/<room_code>/finalize", methods=["POST"])
def finalize(room_code):
    room = load_room(room_code)
    uid = require_host(room)
    result = storage.finalize_room(room["code"], uid)
    return jsonify({
        "status": "success",
        "already_finalized": result["already"],
        "redirect": url_for("results_page", room_code=room["code"]),
    })


# ---------------------------------------------------------------------------
# Results (only after the host finalizes)
# ---------------------------------------------------------------------------

def _results_gate(room: dict):
    me = caller(room)
    if not me:
        return None, _not_linked_page(room)
    if not room["finalized"]:
        return me, _redirect_for(room, me)
    return me, None


@app.route("/room/<room_code>/results")
def results_page(room_code):
    room = load_room(room_code)
    me, blocked = _results_gate(room)
    if blocked:
        return blocked
    bill_split = calculate_bill_split(room)
    return render_template(
        "results.html",
        room=room,
        room_code=room["code"],
        bill_split=bill_split,
        pdf_available=PDF_AVAILABLE,
        caller_name=me["name"],
        caller_is_host=me["is_host"],
        audit_warnings=audit_bill(room, bill_split),
    )


@app.route("/room/<room_code>/download")
def download_pdf(room_code):
    room = load_room(room_code)
    _me, blocked = _results_gate(room)
    if blocked:
        return blocked
    if not PDF_AVAILABLE:
        return error_response("PDF download isn't available on this server.", 503, "NO_PDF")
    try:
        return generate_pdf_response(room, room["code"])
    except Exception as exc:
        print(f"PDF generation error: {exc}")
        return error_response("Couldn't generate the PDF. Please try again.", 500, "PDF_FAILED")


# ---------------------------------------------------------------------------
# Cron (daily, vercel.json): heartbeat write so the free Supabase project is
# never paused for inactivity, then delete expired rooms and old rate limits.
# ---------------------------------------------------------------------------

@app.route("/api/cron/cleanup")
def cron_cleanup():
    import hmac

    secret = os.getenv("CRON_SECRET", "").strip()
    if not secret:
        return jsonify({"status": "error", "message": "CRON_SECRET not configured"}), 503
    provided = request.headers.get("Authorization", "")
    if not hmac.compare_digest(provided.encode(), f"Bearer {secret}".encode()):
        return jsonify({"status": "error", "message": "unauthorized"}), 401
    result = storage.delete_expired()
    print(f"[cron] cleanup {result}")
    return jsonify({"status": "success", **result})


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    port = int(os.getenv("PORT", 5001))
    app.run(debug=debug, host="0.0.0.0", port=port)
