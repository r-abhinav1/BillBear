import base64
import json
import os
import random
import string
import uuid
from datetime import datetime
from io import BytesIO

import qrcode
from dotenv import load_dotenv
from flask import Flask, jsonify, make_response, redirect, render_template, request, url_for
from flask_cors import CORS
from werkzeug.utils import secure_filename

from storage import get_room, room_exists, save_room
from utils.bill_audit import audit_bill
from utils.bill_split import calculate_bill_split
from utils.receipt_contracts import normalize_receipt_payload
from utils.receipt_pipeline import extract_receipt_from_text_payload
from utils.session import (
    clear_session_cookie,
    issue_user_id,
    read_session_cookie,
    set_session_cookie,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------

VERCEL_ENV = os.getenv("VERCEL", False)


def _env_flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


OCR_TEXT_INGESTION_ENABLED = _env_flag("OCR_TEXT_INGESTION_ENABLED", True)
LEGACY_IMAGE_OCR_ENABLED = _env_flag("LEGACY_IMAGE_OCR_ENABLED", False)
CLIENT_OCR_ENABLED = _env_flag("CLIENT_OCR_ENABLED", True)

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
        print("WeasyPrint available for PDF generation")
    except Exception as exc:
        print(f"WeasyPrint not available, trying xhtml2pdf... ({exc})")

if not PDF_AVAILABLE:
    try:
        from xhtml2pdf import pisa
        XHTML2PDF_AVAILABLE = True
        PDF_AVAILABLE = True
        print("xhtml2pdf available for PDF generation")
    except Exception as exc:
        print(f"xhtml2pdf not available ({exc})")

if not PDF_AVAILABLE:
    print("Warning: No PDF libraries available. PDF generation will be disabled.")
elif VERCEL_ENV:
    print("Running on Vercel - using xhtml2pdf for PDF generation")

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)
# CORS is applied only to the API routes; the HTML pages don't need it.
CORS(app, resources={r"/api/*": {"origins": os.getenv("ALLOWED_ORIGINS", "*")}})
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-change-in-production")

if VERCEL_ENV:
    app.config["UPLOAD_FOLDER"] = "/tmp"
else:
    app.config["UPLOAD_FOLDER"] = os.path.join("static", "uploads")
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

# ---------------------------------------------------------------------------
# Small app-level helpers
# ---------------------------------------------------------------------------

def generate_room_code(length: int = 6) -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))


def generate_qr_base64(link: str) -> str:
    qr = qrcode.make(link)
    buf = BytesIO()
    qr.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def parse_text_payload_from_form():
    """Parse the ocr_payload JSON field from the current form submission."""
    raw = request.form.get("ocr_payload", "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid OCR payload JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("OCR payload must be a JSON object")
    return payload


def process_uploaded_image(file) -> dict:
    """
    Run legacy Gemini Vision OCR on an uploaded image file.
    Only used when LEGACY_IMAGE_OCR_ENABLED=true.
    """
    from utils.ocr_image import OcrBillMaker

    extractor = OcrBillMaker()

    try:
        file.seek(0)
        result = extractor.get_text(BytesIO(file.read()))
        return normalize_receipt_payload(result)
    except Exception as memory_error:
        print(f"In-memory OCR failed: {memory_error}. Retrying with temp file.")

    file.seek(0)
    temp_filename = secure_filename(f"{uuid.uuid4().hex}_{file.filename}")
    temp_dir = "/tmp" if VERCEL_ENV else app.config["UPLOAD_FOLDER"]
    temp_path = os.path.join(temp_dir, temp_filename)
    file.save(temp_path)
    try:
        result = extractor.get_text(temp_path)
        return normalize_receipt_payload(result)
    finally:
        try:
            os.remove(temp_path)
        except OSError as exc:
            print(f"Temp file cleanup failed: {exc}")


# ---------------------------------------------------------------------------
# Identity & room-shape helpers (added for session-resilience + host controls)
# ---------------------------------------------------------------------------

def ensure_room_shape(room: dict) -> dict:
    """
    Guarantee a room dict has the fields the new features rely on.
    Old rooms created before these features don't have them — we fill defaults
    in-place so routes can read them without .get() everywhere.
    """
    room.setdefault("users", [])
    room.setdefault("selections", {})
    if not isinstance(room.get("submitted_users"), set):
        room["submitted_users"] = set(room.get("submitted_users", []))
    room.setdefault("user_ids", {})
    room.setdefault("submission_ids", {})
    room.setdefault("last_selections", {})
    room.setdefault("host_user_id", None)
    room.setdefault("schema_version", 1)
    return room


def current_user(room_code: str, room: dict):
    """
    Returns the (user_id, user_name) of the caller for this room, based on
    the cookie. Verifies the user_id is still present in room.user_ids.
    Returns (None, None) if no valid session.
    """
    payload = read_session_cookie(request, room_code)
    if not payload:
        return None, None
    uid = payload.get("uid")
    name = payload.get("name")
    # Cookie must map to a user still registered in this room
    if room.get("user_ids", {}).get(name) != uid:
        return None, None
    return uid, name


def is_host(room: dict, user_name: str) -> bool:
    return bool(user_name) and user_name == room.get("host_name")


def require_host(room_code: str, room: dict) -> bool:
    """True if the caller's cookie identifies them as the room's host."""
    uid, name = current_user(room_code, room)
    if not uid or not name:
        return False
    if not is_host(room, name):
        return False
    # Extra belt-and-braces: host_user_id must match if it was recorded.
    host_uid = room.get("host_user_id")
    if host_uid and host_uid != uid:
        return False
    return True


def generate_pdf_response(room: dict, room_code: str) -> tuple:
    """
    Render results_pdf.html and return (response, status_code).
    Tries WeasyPrint first, falls back to xhtml2pdf.
    """
    bill_split = calculate_bill_split(room)
    html = render_template(
        "results_pdf.html",
        room=room,
        room_code=room_code,
        bill_split=bill_split,
        current_date=datetime.now(),
    )

    buf = BytesIO()
    filename = f'{room["room_name"]}_bill_split.pdf'

    if WEASYPRINT_AVAILABLE and not VERCEL_ENV:
        weasyprint.HTML(string=html).write_pdf(buf)
        buf.seek(0)
        response = make_response(buf.read())
        response.headers["Content-Type"] = "application/pdf"
        response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response, 200

    if XHTML2PDF_AVAILABLE:
        status = pisa.CreatePDF(html, dest=buf)
        if status.err:
            return "Error generating PDF with xhtml2pdf", 500
        buf.seek(0)
        response = make_response(buf.read())
        response.headers["Content-Type"] = "application/pdf"
        response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response, 200

    return "No PDF generation library available", 500


# ---------------------------------------------------------------------------
# Routes — static pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


# ---------------------------------------------------------------------------
# Routes — OCR API
# ---------------------------------------------------------------------------

@app.route("/api/ocr/ingest", methods=["POST"])
def ingest_ocr_text():
    if not OCR_TEXT_INGESTION_ENABLED:
        return jsonify({"status": "error", "message": "OCR text ingestion is disabled"}), 404

    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"status": "error", "message": "Missing JSON payload"}), 400

    try:
        receipt = extract_receipt_from_text_payload(payload)
        receipt = normalize_receipt_payload(receipt)
        parser_meta = receipt.get("parser_meta", {})
        extraction_meta = receipt.get("extraction_meta", {})
        print(
            "OCR ingest: "
            f"source={extraction_meta.get('source', 'unknown')} "
            f"provider={parser_meta.get('fallback_provider', 'none')} "
            f"used_fallback={parser_meta.get('used_fallback', False)} "
            f"confidence={parser_meta.get('confidence', 'N/A')}"
        )
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"status": "error", "message": f"OCR ingestion failed: {exc}"}), 500

    return jsonify({"status": "success", "receipt": receipt})


# ---------------------------------------------------------------------------
# Routes — room creation and editing
# ---------------------------------------------------------------------------

@app.route("/create", methods=["GET", "POST"])
def create_room():
    if request.method == "POST":
        host_name = request.form.get("host_name", "").strip()
        room_name = request.form.get("room_name", "").strip()
        num_people = request.form.get("num_people", "").strip()
        file = request.files.get("bill_image")

        if not host_name or not room_name or not num_people:
            return "Missing required room details", 400

        try:
            num_people_int = int(num_people)
            if not (1 <= num_people_int <= 50):
                return "Number of people must be between 1 and 50", 400
        except ValueError:
            return "Number of people must be a valid integer", 400

        try:
            ocr_result = None
            extraction_source = "unknown"
            skip_ocr = request.form.get("skip_ocr", "").strip().lower() == "true"

            text_payload = parse_text_payload_from_form() if OCR_TEXT_INGESTION_ENABLED else None
            saved_scan = (
                text_payload.get("_vision_receipt") if isinstance(text_payload, dict) else None
            )
            if (
                isinstance(saved_scan, dict)
                and saved_scan.get("items")
                and saved_scan.get("extraction_meta", {}).get("source") == "user_saved_json"
            ):
                # Saved-JSON short-circuit: trust the user's uploaded scan.
                # Missing subtotal/total are allowed here — edit_items lets the
                # user fix them before proceeding.
                ocr_result = normalize_receipt_payload(saved_scan)
                ocr_result["parser_meta"] = saved_scan.get("parser_meta", {
                    "source": "user_saved_json",
                    "used_fallback": False,
                })
                ocr_result["extraction_meta"] = saved_scan.get("extraction_meta", {
                    "mode": "saved_json",
                    "source": "user_saved_json",
                    "provider": "none",
                })
                extraction_source = "user_saved_json"
            elif text_payload:
                ocr_result = extract_receipt_from_text_payload(text_payload)
                ocr_result = normalize_receipt_payload(ocr_result)
                extraction_source = ocr_result.get("extraction_meta", {}).get("source", "ocr_text")
            elif LEGACY_IMAGE_OCR_ENABLED and file and file.filename:
                ocr_result = process_uploaded_image(file)
                extraction_source = "legacy_image_ocr"
                ocr_result["extraction_meta"] = {
                    "mode": "image",
                    "source": extraction_source,
                    "provider": "gemini",
                }
            elif skip_ocr:
                ocr_result = {
                    "items": [],
                    "extraction_meta": {"mode": "manual", "source": "user_skipped_ocr"},
                }
                extraction_source = "manual"
            else:
                if OCR_TEXT_INGESTION_ENABLED and not LEGACY_IMAGE_OCR_ENABLED:
                    return "No OCR text payload provided and legacy image OCR is disabled", 400
                return "No file uploaded", 400

            items = ocr_result.get("items", [])
            parser_meta = ocr_result.get("parser_meta", {})
            extraction_meta = ocr_result.get("extraction_meta", {})
            print(
                "Create room OCR: "
                f"source={extraction_meta.get('source', extraction_source)} "
                f"provider={parser_meta.get('fallback_provider', 'none')} "
                f"used_fallback={parser_meta.get('used_fallback', False)} "
                f"confidence={parser_meta.get('confidence', 'N/A')}"
            )

            room_code = generate_room_code()
            host_uid = issue_user_id()
            room_data = {
                "host_name": host_name,
                "room_name": room_name,
                "num_people": num_people_int,
                "bill_image": None,
                "items": items,
                "ocr_data": ocr_result,
                "ocr_source": extraction_source,
                "users": [host_name],
                "selections": {},
                "submitted_users": set(),
                # Session-resilience fields (schema v2)
                "user_ids": {host_name: host_uid},
                "host_user_id": host_uid,
                "submission_ids": {},
                "last_selections": {},
                "schema_version": 2,
            }

            if not save_room(room_code, room_data):
                return "Error saving room data", 500

            response = make_response(
                render_template("edit_items.html", room_code=room_code, items=ocr_result)
            )
            set_session_cookie(response, room_code, host_uid, host_name)
            return response

        except ValueError as exc:
            return f"Invalid request: {exc}", 400
        except Exception as exc:
            print(f"Error processing file: {exc}")
            return f"Error processing uploaded file: {exc}", 500

    mode = (request.args.get("mode") or "image").lower()
    if mode not in {"image", "json"}:
        mode = "image"
    return render_template(
        "create_room.html",
        client_ocr_enabled=CLIENT_OCR_ENABLED and OCR_TEXT_INGESTION_ENABLED,
        legacy_image_ocr_enabled=LEGACY_IMAGE_OCR_ENABLED,
        mode=mode,
    )


@app.route("/edit/<room_code>", methods=["GET", "POST"])
def edit_items(room_code):
    room = get_room(room_code)
    if not room:
        return "Room not found", 404

    if request.method == "POST":
        names = request.form.getlist("item_name")
        prices = request.form.getlist("item_price")
        items = [
            {"name": name.strip(), "price": price}
            for name, price in zip(names, prices)
            if name.strip()
        ]

        def _safe_form_value(field: str, default: str = "N/A") -> str:
            value = request.form.get(field, default).strip()
            return value or default

        if "ocr_data" not in room:
            room["ocr_data"] = {}

        room["ocr_data"].update({
            "subtotal": _safe_form_value("subtotal"),
            "serviceCharge": _safe_form_value("serviceCharge"),
            "discount": _safe_form_value("discount"),
            "cgst": _safe_form_value("cgst"),
            "sgst": _safe_form_value("sgst"),
            "igst": _safe_form_value("igst"),
            "total": _safe_form_value("total"),
        })

        room["items"] = items
        room["ocr_data"]["items"] = items

        if not save_room(room_code, room):
            return "Error saving room data", 500

        return redirect(url_for("room_summary", room_code=room_code))

    ocr_data = normalize_receipt_payload(room.get("ocr_data", {}))
    return render_template("edit_items.html", room_code=room_code, items=ocr_data)


# ---------------------------------------------------------------------------
# Routes — room lobby and status
# ---------------------------------------------------------------------------

@app.route("/room/<room_code>")
def room_summary(room_code):
    room = get_room(room_code)
    if not room:
        return "Room not found", 404

    if "users" not in room:
        room["users"] = []
    if room["host_name"] not in room["users"]:
        room["users"].insert(0, room["host_name"])

    room.setdefault("selections", {})
    room.setdefault("submitted_users", set())
    save_room(room_code, room)

    join_url = url_for("join_room", room_code=room_code, _external=True)
    qr_b64 = generate_qr_base64(join_url)
    return render_template("room_summary.html", room=room, room_code=room_code, qr_b64=qr_b64, join_url=join_url)


@app.route("/room/<room_code>/status")
def room_status(room_code):
    room = get_room(room_code)
    if not room:
        return jsonify({"error": "Room not found"}), 404
    ensure_room_shape(room)

    users = room.get("users", [])
    submitted_users = list(room.get("submitted_users", set()))
    expected_people = int(room.get("num_people", 1))
    enough_joined = len(users) >= expected_people
    all_submitted = len(submitted_users) == len(users) > 0

    # Caller identity (for kicked-user detection on the client)
    _, caller_name = current_user(room_code, room)
    caller_in_room = bool(caller_name) and caller_name in users
    caller_is_host = is_host(room, caller_name) if caller_name else False

    return jsonify({
        "status": "success",
        "users": users,
        "submitted_users": submitted_users,
        "all_submitted": all_submitted,
        "enough_users_joined": enough_joined,
        "ready_to_proceed": enough_joined and all_submitted,
        "total_users": len(users),
        "expected_people": expected_people,
        "submitted_count": len(submitted_users),
        "host_name": room.get("host_name", ""),
        "caller_name": caller_name,
        "caller_in_room": caller_in_room,
        "caller_is_host": caller_is_host,
    })


# ---------------------------------------------------------------------------
# Routes — joining a room
# ---------------------------------------------------------------------------

@app.route("/join", methods=["GET", "POST"])
def join():
    if request.method == "POST":
        user_name = request.form.get("user_name", "").strip()
        room_code = request.form.get("room_code", "").strip().upper()

        if not user_name:
            return render_template("join_room.html", error="Please enter your name")
        if not room_code:
            return render_template("join_room.html", error="Please enter a room code")
        if not room_exists(room_code):
            return render_template("join_room.html", error="Room not found. Please check the room code.")

        room = get_room(room_code)
        if not room:
            return render_template("join_room.html", error="Room not found. Please check the room code.")
        ensure_room_shape(room)

        # Cookie reclaim: if the caller already has a valid session for this
        # room, send them back to their existing state instead of creating a
        # duplicate entry. This is the primary fix for the "submit appeared
        # to fail, user rejoined and got a duplicate" bug.
        _, cookie_name = current_user(room_code, room)
        if cookie_name and cookie_name in room.get("users", []):
            return redirect(url_for("user_room", room_code=room_code, user_name=cookie_name))

        if user_name in room.get("users", []):
            return render_template(
                "join_room.html",
                error=f"'{user_name}' already joined from another device. Open that device's original link to resume, or pick a different name.",
            )
        if len(room.get("users", [])) >= int(room.get("num_people", 1)):
            return render_template("join_room.html", error="Room is full.")

        uid = issue_user_id()
        room.setdefault("users", []).append(user_name)
        room["user_ids"][user_name] = uid
        if not save_room(room_code, room):
            return render_template("join_room.html", error="Error joining room. Please try again.")

        response = make_response(
            redirect(url_for("user_room", room_code=room_code, user_name=user_name))
        )
        set_session_cookie(response, room_code, uid, user_name)
        return response

    return render_template("join_room.html")


@app.route("/join/<room_code>", methods=["GET", "POST"])
def join_room(room_code):
    room_code = room_code.upper()
    room = get_room(room_code)
    if not room:
        return render_template("join_room_direct.html", error="Room not found", room_code=room_code)
    ensure_room_shape(room)

    # Cookie reclaim: if the user already holds a valid session for this room,
    # jump them straight back to their current state (select-items / waiting)
    # regardless of whether this is a GET or POST.
    _, cookie_name = current_user(room_code, room)
    if cookie_name and cookie_name in room.get("users", []):
        return redirect(url_for("user_room", room_code=room_code, user_name=cookie_name))

    if request.method == "POST":
        user_name = request.form.get("user_name", "").strip()

        if not user_name:
            return render_template("join_room_direct.html", error="Please enter your name", room=room, room_code=room_code)
        if user_name in room.get("users", []):
            return render_template(
                "join_room_direct.html",
                error=f"'{user_name}' already joined from another device. Open that device's original link to resume, or pick a different name.",
                room=room,
                room_code=room_code,
            )
        if len(room.get("users", [])) >= int(room.get("num_people", 1)):
            return render_template("join_room_direct.html", error="Room is full.", room=room, room_code=room_code)

        uid = issue_user_id()
        room.setdefault("users", []).append(user_name)
        room["user_ids"][user_name] = uid
        if not save_room(room_code, room):
            return render_template("join_room_direct.html", error="Error joining room. Please try again.", room=room, room_code=room_code)

        print(f"User '{user_name}' joined room '{room_code}' ({room['room_name']})")
        response = make_response(
            redirect(url_for("user_room", room_code=room_code, user_name=user_name))
        )
        set_session_cookie(response, room_code, uid, user_name)
        return response

    return render_template("join_room_direct.html", room=room, room_code=room_code)


# ---------------------------------------------------------------------------
# Routes — user item selection and waiting
# ---------------------------------------------------------------------------

@app.route("/room/<room_code>/user/<user_name>")
def user_room(room_code, user_name):
    room = get_room(room_code)
    if not room:
        return "Room not found", 404

    if user_name in room.get("submitted_users", set()):
        return redirect(url_for("waiting_room", room_code=room_code, user_name=user_name))

    return redirect(url_for("select_items", room_code=room_code, user_name=user_name))


@app.route("/room/<room_code>/user/<user_name>/select-items", methods=["GET", "POST"])
def select_items(room_code, user_name):
    room = get_room(room_code)
    if not room:
        return "Room not found", 404
    ensure_room_shape(room)

    # Cookie > URL: if a session exists for a different name, redirect to
    # that user's own URL. Prevents accidental spoofing via URL-editing and
    # also recovers users who landed on the wrong URL.
    _, cookie_name = current_user(room_code, room)
    if cookie_name and cookie_name != user_name:
        return redirect(url_for("select_items", room_code=room_code, user_name=cookie_name))

    # Pre-fill previous selections so revoke-and-reselect doesn't wipe state.
    prev_selections = list(
        room.get("selections", {}).get(user_name)
        or room.get("last_selections", {}).get(user_name)
        or []
    )

    wants_json = (
        request.accept_mimetypes.best == "application/json"
        or request.headers.get("X-Requested-With") == "fetch"
        or request.form.get("ajax") == "1"
    )

    if request.method == "POST":
        selected_items = request.form.getlist("selected_items")
        submission_id = request.form.get("submission_id", "").strip()

        existing_submission_id = room.get("submission_ids", {}).get(user_name)
        if submission_id and existing_submission_id == submission_id:
            # Idempotent retry: server already has this exact submission.
            # Treat as success without re-writing state.
            print(f"Idempotent submit no-op for {user_name} (submission_id={submission_id[:8]}…)")
            if wants_json:
                return jsonify({
                    "status": "success",
                    "idempotent": True,
                    "redirect": url_for("waiting_room", room_code=room_code, user_name=user_name),
                })
            return redirect(url_for("waiting_room", room_code=room_code, user_name=user_name))

        room["selections"][user_name] = selected_items
        room["submitted_users"].add(user_name)
        if submission_id:
            room["submission_ids"][user_name] = submission_id
        # Also stash into last_selections so a future revoke→edit has a draft
        room["last_selections"][user_name] = list(selected_items)

        if not save_room(room_code, room):
            if wants_json:
                return jsonify({"status": "error", "message": "Error saving selections."}), 500
            return "Error saving selections. Please try again.", 500

        print(f"User {user_name} selected: {selected_items}")
        if wants_json:
            return jsonify({
                "status": "success",
                "idempotent": False,
                "redirect": url_for("waiting_room", room_code=room_code, user_name=user_name),
            })
        return redirect(url_for("waiting_room", room_code=room_code, user_name=user_name))

    # GET: if already submitted AND there's no cookie session asking to edit,
    # send them to the waiting room. The revoke flow clears submitted_users
    # before sending them here, so users who landed here intentionally edit.
    if user_name in room.get("submitted_users", set()):
        return redirect(url_for("waiting_room", room_code=room_code, user_name=user_name))

    return render_template(
        "select_items.html",
        room=room,
        room_code=room_code,
        user_name=user_name,
        pre_selected=prev_selections,
    )


@app.route("/api/room/<room_code>/my-status")
def my_status(room_code):
    """
    Client-side resilience endpoint. After a submit whose response was lost
    (flaky network, backgrounded mobile tab), the client calls this to check
    whether the server actually persisted its submission — rather than
    showing a confusing error and driving the user to rejoin with a new name.
    """
    room = get_room(room_code)
    if not room:
        return jsonify({"status": "error", "message": "Room not found"}), 404
    ensure_room_shape(room)

    uid, name = current_user(room_code, room)
    if not uid or not name:
        return jsonify({
            "status": "success",
            "authenticated": False,
            "in_room": False,
            "submitted": False,
        })

    in_room = name in room.get("users", [])
    submitted = name in room.get("submitted_users", set())
    selections = room.get("selections", {}).get(name, [])
    submission_id = room.get("submission_ids", {}).get(name, "")

    return jsonify({
        "status": "success",
        "authenticated": True,
        "user_name": name,
        "in_room": in_room,
        "submitted": submitted,
        "selections": selections,
        "submission_id": submission_id,
        "redirect": (
            url_for("waiting_room", room_code=room_code, user_name=name) if submitted
            else url_for("select_items", room_code=room_code, user_name=name) if in_room
            else url_for("index")
        ),
    })


@app.route("/room/<room_code>/waiting")
@app.route("/room/<room_code>/waiting/<user_name>")
def waiting_room(room_code, user_name=None):
    room = get_room(room_code)
    if not room:
        return "Room not found", 404

    if not user_name:
        user_name = request.args.get("user", "")

    join_url = url_for("join_room", room_code=room_code, _external=True)
    qr_b64 = generate_qr_base64(join_url)

    return render_template(
        "waiting_room.html",
        room=room,
        room_code=room_code,
        user_name=user_name,
        is_host=(user_name == room.get("host_name", "")),
        qr_b64=qr_b64,
        join_url=join_url,
    )


# ---------------------------------------------------------------------------
# Routes — results and host controls
# ---------------------------------------------------------------------------

@app.route("/room/<room_code>/force-complete/<user_name>", methods=["POST"])
def force_complete(room_code, user_name):
    room = get_room(room_code)
    if not room:
        return "Room not found", 404
    if user_name != room.get("host_name"):
        return "Only the host can force completion", 403

    users = room.get("users", [])
    submitted = room.get("submitted_users", set())
    selections = room.get("selections", {})

    for user in users:
        if user not in submitted:
            selections[user] = []
            submitted.add(user)

    room["selections"] = selections
    room["submitted_users"] = submitted

    if not save_room(room_code, room):
        return "Error saving room data", 500

    print(f"Host {user_name} forced completion for room {room_code}")
    return redirect(url_for("results_page", room_code=room_code))


# ---------------------------------------------------------------------------
# Routes — self-service revoke, host controls (added for session-resilience)
# ---------------------------------------------------------------------------

@app.route("/room/<room_code>/user/<user_name>/revoke", methods=["POST"])
def revoke_own_selection(room_code, user_name):
    """
    Let a guest un-submit their own selection so they can re-pick items.
    Must come from the same cookie that originally submitted.
    """
    room = get_room(room_code)
    if not room:
        return jsonify({"status": "error", "message": "Room not found"}), 404
    ensure_room_shape(room)

    _, cookie_name = current_user(room_code, room)
    if cookie_name != user_name:
        return jsonify({"status": "error", "message": "Not your session."}), 403
    if user_name not in room.get("users", []):
        return jsonify({"status": "error", "message": "You are no longer in this room."}), 404

    # Preserve the user's last selection so the edit screen can pre-check it.
    if user_name in room.get("selections", {}):
        room["last_selections"][user_name] = list(room["selections"][user_name])
    room.get("submitted_users", set()).discard(user_name)
    room.get("selections", {}).pop(user_name, None)
    # Clear the submission_id so the resubmit doesn't collide with the prior one.
    room.get("submission_ids", {}).pop(user_name, None)

    if not save_room(room_code, room):
        return jsonify({"status": "error", "message": "Save failed. Try again."}), 500

    print(f"User {user_name} revoked own selection in {room_code}")
    return jsonify({
        "status": "success",
        "redirect": url_for("select_items", room_code=room_code, user_name=user_name),
    })


@app.route("/room/<room_code>/host/remove-user", methods=["POST"])
def host_remove_user(room_code):
    """Host removes another participant (e.g. accidental duplicate)."""
    room = get_room(room_code)
    if not room:
        return jsonify({"status": "error", "message": "Room not found"}), 404
    ensure_room_shape(room)

    if not require_host(room_code, room):
        return jsonify({"status": "error", "message": "Host only."}), 403

    target = (request.get_json(silent=True) or request.form).get("target_user", "").strip()
    if not target:
        return jsonify({"status": "error", "message": "Missing target_user"}), 400
    if target == room.get("host_name"):
        return jsonify({"status": "error", "message": "Cannot remove the host."}), 400
    if target not in room.get("users", []):
        return jsonify({"status": "error", "message": "User not in room."}), 404

    room["users"] = [u for u in room["users"] if u != target]
    room.get("selections", {}).pop(target, None)
    room.get("submitted_users", set()).discard(target)
    room.get("user_ids", {}).pop(target, None)
    room.get("submission_ids", {}).pop(target, None)
    room.get("last_selections", {}).pop(target, None)

    if not save_room(room_code, room):
        return jsonify({"status": "error", "message": "Save failed."}), 500

    print(f"Host removed user {target} from {room_code}")
    return jsonify({"status": "success", "removed": target})


@app.route("/room/<room_code>/host/num-people", methods=["POST"])
def host_update_num_people(room_code):
    """Host adjusts the expected headcount for the room."""
    room = get_room(room_code)
    if not room:
        return jsonify({"status": "error", "message": "Room not found"}), 404
    ensure_room_shape(room)

    if not require_host(room_code, room):
        return jsonify({"status": "error", "message": "Host only."}), 403

    data = request.get_json(silent=True) or request.form
    try:
        new_value = int(str(data.get("num_people", "")).strip())
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "num_people must be an integer"}), 400

    if new_value < 1 or new_value > 50:
        return jsonify({"status": "error", "message": "num_people must be between 1 and 50"}), 400
    if new_value < len(room.get("users", [])):
        return jsonify({
            "status": "error",
            "message": f"Cannot set below current joined count ({len(room.get('users', []))}). Remove users first.",
        }), 400

    room["num_people"] = new_value
    if not save_room(room_code, room):
        return jsonify({"status": "error", "message": "Save failed."}), 500

    print(f"Host set num_people={new_value} on {room_code}")
    return jsonify({"status": "success", "num_people": new_value})


@app.route("/room/<room_code>/host/reopen-selections", methods=["POST"])
def host_reopen_selections(room_code):
    """
    Revert the computed split: move everyone back to the selections stage so
    they can modify their picks. Preserves each user's prior selection into
    last_selections so their boxes start pre-checked.
    """
    room = get_room(room_code)
    if not room:
        return jsonify({"status": "error", "message": "Room not found"}), 404
    ensure_room_shape(room)

    if not require_host(room_code, room):
        return jsonify({"status": "error", "message": "Host only."}), 403

    # Snapshot each user's selections into last_selections before clearing.
    for user, picks in list(room.get("selections", {}).items()):
        room["last_selections"][user] = list(picks or [])

    room["selections"] = {}
    room["submitted_users"] = set()
    room["submission_ids"] = {}

    if not save_room(room_code, room):
        return jsonify({"status": "error", "message": "Save failed."}), 500

    print(f"Host reopened selections on {room_code}")
    return jsonify({"status": "success"})


@app.route("/room/<room_code>/host/revoke/<target_user>", methods=["POST"])
def host_revoke_user(room_code, target_user):
    """Host forces another user back to unsubmitted so they can re-pick."""
    room = get_room(room_code)
    if not room:
        return jsonify({"status": "error", "message": "Room not found"}), 404
    ensure_room_shape(room)

    if not require_host(room_code, room):
        return jsonify({"status": "error", "message": "Host only."}), 403
    if target_user not in room.get("users", []):
        return jsonify({"status": "error", "message": "User not in room."}), 404

    if target_user in room.get("selections", {}):
        room["last_selections"][target_user] = list(room["selections"][target_user])
    room.get("submitted_users", set()).discard(target_user)
    room.get("selections", {}).pop(target_user, None)
    room.get("submission_ids", {}).pop(target_user, None)

    if not save_room(room_code, room):
        return jsonify({"status": "error", "message": "Save failed."}), 500

    print(f"Host revoked submission for {target_user} in {room_code}")
    return jsonify({"status": "success", "revoked": target_user})


@app.route("/room/<room_code>/results")
def results_page(room_code):
    room = get_room(room_code)
    if not room:
        return "Room not found", 404
    ensure_room_shape(room)

    _, caller_name = current_user(room_code, room)
    caller_is_host = is_host(room, caller_name) if caller_name else False

    bill_split = calculate_bill_split(room)
    audit_warnings = audit_bill(room, bill_split)
    return render_template(
        "results.html",
        room=room,
        room_code=room_code,
        bill_split=bill_split,
        pdf_available=PDF_AVAILABLE,
        caller_name=caller_name or "",
        caller_is_host=caller_is_host,
        audit_warnings=audit_warnings,
    )


@app.route("/room/<room_code>/download")
def download_pdf(room_code):
    room = get_room(room_code)
    if not room:
        return "Room not found", 404
    if not PDF_AVAILABLE:
        return "PDF generation not available. Install weasyprint or xhtml2pdf.", 500

    try:
        response, status = generate_pdf_response(room, room_code)
        return response, status
    except Exception as exc:
        print(f"PDF generation error: {exc}")
        return f"Error generating PDF: {exc}", 500


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    port = int(os.getenv("PORT", 5001))
    app.run(debug=debug, host="0.0.0.0", port=port)
