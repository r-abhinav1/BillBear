"""Receipt scan (vision only). No room is touched here."""
from flask import Blueprint, g, jsonify, request

from billbear import storage
from billbear.bill.money import get_currency
from billbear.bill.totals import as_number
from billbear.bill.validation import clean_items, MAX_ITEMS
from billbear.scan.contracts import normalize_receipt_payload
from billbear.scan.image_prep import ImageRejected, prepare_receipt_image
from billbear.scan.pipeline import extract_receipt_from_vision, VisionFailed, VisionUnavailable
from billbear.web.access import rate_limited
from billbear.web.errors import error_response
from billbear.web.observability import capture, count, distribution
from billbear.web.security import client_ip, scan_daily_cap, turnstile_enabled, verify_turnstile
from billbear.web.session import has_valid_scan_ticket

bp = Blueprint("scan", __name__)


def _daily_cap_response():
    return error_response(
        "Bill scanning has hit today's limit. Please enter items manually — it resets tomorrow.",
        429,
        "DAILY_CAP",
    )


@bp.route("/api/receipt/scan", methods=["POST"])
def scan_receipt():
    """The scan, plus its metrics: every exit below records its outcome in g.scan."""
    import time

    g.scan = {"outcome": "error"}
    started = time.monotonic()
    try:
        return _scan_receipt()
    finally:
        scan = g.scan
        attrs = {"outcome": scan["outcome"], "provider": scan.get("provider")}
        count("scan.result", currency=scan.get("currency"), currency_confirmed=scan.get("confirmed"), **attrs)
        if scan.get("model_called"):
            distribution("scan.duration", (time.monotonic() - started) * 1000, "millisecond", **attrs)
        if "dishes" in scan:
            distribution("scan.dishes", scan["dishes"], None, provider=scan.get("provider"))


def _scan_receipt():
    if not has_valid_scan_ticket(request):
        g.scan["outcome"] = "no_ticket"
        return error_response("Scan session expired. Reload the page and try again.", 403, "NO_TICKET")
    if not verify_turnstile(request.form.get("cf-turnstile-response", ""), client_ip(request), "scan"):
        g.scan["outcome"] = "bot_check"
        return error_response("Bot check failed. Reload the page and try again.", 403, "BOT_CHECK")
    g.human_verified = turnstile_enabled()
    if rate_limited("scan") or rate_limited("scan_day"):
        g.scan["outcome"] = "rate_limited"
        return error_response(
            "Too many scans from this network. Wait a bit, or enter items manually.", 429, "RATE_LIMITED"
        )
    # Global budget ceiling for the whole site. Peek first: once today's allowance
    # is gone, turn callers away before spending any effort decoding their image.
    cap = scan_daily_cap()
    if not storage.rate_limit_peek("scan_global:all", cap, 86400):
        g.scan["outcome"] = "daily_cap"
        return _daily_cap_response()

    upload = request.files.get("image")
    if not upload:
        g.scan["outcome"] = "no_image"
        return error_response("No image uploaded.", 400, "NO_IMAGE")
    try:
        image_b64, mime = prepare_receipt_image(upload.read())
    except ImageRejected as exc:
        g.scan["outcome"] = "bad_image"
        return error_response(str(exc), 400, "BAD_IMAGE")
    # Spend a slot only for a real image that is about to reach the paid vision API,
    # so junk uploads can't use up everyone's daily allowance.
    if not storage.rate_limit_hit("scan_global:all", cap, 86400):
        g.scan["outcome"] = "daily_cap"
        return _daily_cap_response()

    g.scan["model_called"] = True
    try:
        receipt = extract_receipt_from_vision(image_b64, mime)
    except VisionUnavailable as exc:
        # Not configured, or the provider account can't pay: someone has to act.
        g.scan["outcome"] = "unavailable"
        capture(exc, kind="scan_unavailable")
        return error_response(str(exc), 503, "SCAN_UNAVAILABLE")
    except VisionFailed as exc:
        g.scan["outcome"] = "failed"
        return error_response(
            f"{exc} Try a clearer, well-lit photo, or enter items manually.", 502, "SCAN_FAILED"
        )

    # The pipeline already normalizes; again here, so a stubbed or older
    # provider result has the same shape. A model can't mark its own currency
    # as confirmed: only the host's tap does.
    receipt = normalize_receipt_payload(receipt)
    currency = get_currency(receipt["currency"]["code"])
    raw_items = receipt.get("items") if isinstance(receipt.get("items"), list) else []
    try:
        items = clean_items(raw_items, currency=currency)
    except ValueError:
        # More lines than a bill can hold. The scan is already paid for, so keep
        # the first MAX_ITEMS and tell the host, rather than fail the whole scan.
        items = clean_items(raw_items[:MAX_ITEMS], currency=currency)
        receipt["dropped"] = len(raw_items) - MAX_ITEMS
    receipt["items"] = [
        {"name": i["name"], "price": as_number(i["price"]), "category": i["category"],
         "quantity": i["quantity"]}
        for i in items
    ]
    receipt.pop("parser_meta", None)
    g.scan.update({
        "outcome": "success",
        "provider": (receipt.get("extraction_meta") or {}).get("provider"),
        "currency": receipt["currency"]["code"],
        "confirmed": receipt["currency"]["confirmed"],
        "dishes": len(items),
    })
    return jsonify({"status": "success", "receipt": receipt})
