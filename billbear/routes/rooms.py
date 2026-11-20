"""Making a room, editing its bill (host only), and the room's live status."""
import json
from itertools import zip_longest

from flask import Blueprint, g, jsonify, make_response, redirect, render_template, request, url_for

from billbear import storage
from billbear.bill.money import DEFAULT_CURRENCY, format_money, get_currency, is_supported
from billbear.bill.totals import as_number, clean_charges, read_bill, starter_rows, stored_charges
from billbear.bill.validation import (
    clean_items,
    MAX_PEOPLE,
    MIN_PEOPLE,
    normalize_person_name,
    normalize_quantity,
    normalize_room_name,
    parse_price,
)
from billbear.scan.contracts import normalize_item_category, normalize_receipt_payload
from billbear.web.access import caller, load_room, rate_limited, require_host
from billbear.web.observability import count, count_sampled, distribution
from billbear.web.security import client_ip, turnstile_enabled, turnstile_site_key, verify_turnstile
from billbear.web.session import (
    clear_human_pass,
    has_human_pass,
    set_hostkey_cookie,
    set_scan_ticket,
    set_session_cookie,
)
from billbear.web.sharing import generate_qr_base64, host_recovery_url, share_url

bp = Blueprint("rooms", __name__)


def _receipt_totals(normalized: dict) -> dict:
    """A normalized receipt -> what rooms.ocr_data keeps (everything but the dishes)."""
    totals = {
        "currency": normalized["currency"]["code"],
        "subtotal": normalized.get("subtotal"),
        "total": normalized.get("total"),
        "charges": normalized.get("charges") or [],
    }
    if normalized.get("taxSplit") in ("equal", "proportional"):
        totals["taxSplit"] = normalized["taxSplit"]
    for field in ("restaurant", "date", "time"):
        value = normalized.get(field)
        if isinstance(value, str) and value:
            totals[field] = value[:120]
    return totals


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


@bp.route("/create", methods=["GET", "POST"])
def create_room():
    mode = (request.values.get("mode") or "image").lower()
    if mode not in {"image", "json"}:
        mode = "image"
    if request.method == "GET":
        return _render_create(mode)

    form = request.form
    if rate_limited("create"):
        return _render_create(mode, "Too many rooms created from this network. Try again later.", 429, form)
    # A scan in the last 15 minutes already passed the bot check (its token is
    # used up), so that stands in for a fresh token here.
    if not (g.get("load_test") or has_human_pass(request)) and not verify_turnstile(
        form.get("cf-turnstile-response", ""), client_ip(request), "create"
    ):
        return _render_create(mode, "Bot check failed. Wait a moment, then tap the button again.", 403, form)

    try:
        host_name = normalize_person_name(form.get("host_name"))
        room_name = normalize_room_name(form.get("room_name"))
        try:
            num_people = int(form.get("num_people", "").strip())
        except ValueError:
            raise ValueError("Number of people must be a whole number.")
        if not MIN_PEOPLE <= num_people <= MAX_PEOPLE:
            raise ValueError(f"Number of people must be between {MIN_PEOPLE} and {MAX_PEOPLE}.")

        if form.get("skip_ocr", "").lower() == "true":
            receipt, source = {}, "manual"
        else:
            raw = form.get("receipt_json", "").strip()
            if not raw:
                raise ValueError("Scan the bill first, or tap 'Enter items manually'.")
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, RecursionError):
                # RecursionError: thousands of nested brackets, never a real scan.
                raise ValueError("The scanned receipt data was corrupted. Please scan again.")
            if not isinstance(parsed, dict):
                raise ValueError("The scanned receipt data was invalid. Please scan again.")
            receipt = parsed.get("_vision_receipt") if isinstance(parsed.get("_vision_receipt"), dict) else parsed
            meta_source = (receipt.get("extraction_meta") or {}).get("source")
            source = meta_source if meta_source in {"vision_llm", "user_saved_json"} else "client_json"

        # The host's browser only posts a receipt once its currency is confirmed
        # (or a saved scan, whose currency the host already chose).
        normalized = normalize_receipt_payload(receipt, trust_confirmation=True)
        items = clean_items(normalized.get("items"), currency=get_currency(normalized["currency"]["code"]))
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
    count("room.created", source=source, currency=normalized["currency"]["code"])
    distribution("room.dishes", len(items), None, source=source)
    distribution("room.people_expected", num_people)
    response = make_response(redirect(url_for("rooms.edit_items", room_code=code)))
    set_session_cookie(response, code, host_uid, host_name)
    set_hostkey_cookie(response, code, recovery_token)
    clear_human_pass(response)      # one room per bot check; the next one needs a new pass
    return response


def _edited_totals(form, rows: list, charge_rows: list) -> dict:
    """
    The edit form -> ocr_data totals (plus the cleaned dishes under "_items").
    Raises ValueError with a message for the host on anything it can't use.
    """
    code = (form.get("currency") or DEFAULT_CURRENCY).strip().upper()
    if not is_supported(code):
        raise ValueError("Pick a currency from the list.")
    currency = get_currency(code)
    items = clean_items(rows, strict_prices=True, currency=currency)
    amounts = {}
    for field, label in (("subtotal", "Items subtotal"), ("total", "Bill total")):
        try:
            amounts[field] = parse_price(form.get(field), currency, strict=True)
        except ValueError as exc:
            raise ValueError(f"{label}: {exc}")
    base = amounts["subtotal"]
    if base is None:
        base = sum((i["price"] for i in items if i["price"] is not None), start=parse_price(0))
    charges = clean_charges(charge_rows, currency, strict=True, percent_base=base)
    totals = {
        "_items": items,
        "currency": code,
        "subtotal": as_number(amounts["subtotal"]),
        "total": as_number(amounts["total"]),
        "charges": stored_charges(charges),
    }
    # The switch is a checkbox, so "off" arrives as a missing field; the
    # marker says the form had the switch at all.
    if "tax_split_set" in form:
        totals["taxSplit"] = "proportional" if form.get("tax_proportional") else "equal"
    return totals


def _edit_view(room: dict) -> dict:
    """What the edit screen shows for a room: amounts as the host would type them."""
    bill = read_bill(room["ocr_data"])
    currency = bill["currency"]

    def shown(amount):
        return "" if amount is None else format_money(amount, currency)

    charges = [
        {"label": c["label"], "amount": shown(c["amount"]), "kind": c["kind"]} for c in bill["charges"]
    ]
    if not charges:
        # Empty starter rows (CGST, SGST, Service charge for rupees). Rows left
        # empty are not saved.
        charges = [dict(row, amount="", starter=True) for row in starter_rows(currency.code)]
    return {
        "currency": currency.code,
        "subtotal": shown(bill["subtotal"]),
        "total": shown(bill["total"]),
        "charges": charges,
        "taxSplit": bill["tax_split"],
        "items": [
            {"id": i["id"], "name": i["raw_name"], "price": shown(i["amount"]), "category": i["category"],
             "quantity": i["quantity"]}
            for i in room["items"]
        ],
    }


@bp.route("/edit/<room_code>", methods=["GET", "POST"])
def edit_items(room_code):
    room = load_room(room_code)
    require_host(room)
    code = room["code"]

    if room["finalized"]:
        return render_template(
            "message.html",
            title="Bill is finalized",
            message="Reopen selections from the results page before editing items.",
            link=url_for("results.results_page", room_code=code),
            link_text="Go to results",
        ), 409

    error = ""
    form = request.form
    if request.method == "POST":
        ids = form.getlist("item_id")
        names = form.getlist("item_name")
        prices = form.getlist("item_price")
        categories = form.getlist("item_category")
        quantities = form.getlist("item_quantity")
        rows = [
            {"id": item_id, "name": name, "price": price, "category": category, "quantity": quantity}
            for item_id, name, price, category, quantity in zip_longest(ids, names, prices, categories, quantities)
            if name is not None
        ]
        charge_rows = [
            {"label": label, "amount": amount, "kind": kind}
            for label, amount, kind in zip_longest(
                form.getlist("charge_label"), form.getlist("charge_amount"), form.getlist("charge_kind")
            )
        ]
        try:
            totals = _edited_totals(form, rows, charge_rows)
            items = totals.pop("_items")
        except ValueError as exc:
            error = str(exc)
        else:
            storage.replace_items(code, items, totals)
            return redirect(url_for("rooms.room_summary", room_code=code))

    if error:
        # Re-show what the host typed so one bad amount doesn't wipe their edits.
        view = {
            "currency": form.get("currency") if is_supported(form.get("currency")) else room["currency"]["code"],
            "subtotal": form.get("subtotal", ""),
            "total": form.get("total", ""),
            "charges": [
                {"label": r["label"] or "", "amount": r["amount"] or "",
                 "kind": "discount" if r["kind"] == "discount" else "charge"}
                for r in charge_rows
            ],
            "taxSplit": "proportional" if form.get("tax_proportional") else "equal",
            "items": [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "price": r["price"] or "",
                    "category": normalize_item_category(r["category"]),
                    "quantity": normalize_quantity(r["quantity"]),
                }
                for r in rows
            ],
        }
    else:
        view = _edit_view(room)
    return render_template(
        "edit_items.html",
        room_code=code,
        room_name=room["room_name"],
        items=view,
        currency=get_currency(view["currency"]).public(),
        error=error,
        submitted_count=len(room["submitted_users"]),
        manual=room["ocr_source"] == "manual",
    ), (400 if error else 200)


@bp.route("/room/<room_code>")
def room_summary(room_code):
    room = load_room(room_code)
    me = caller(room)
    join_url = share_url("join.join_room", room_code=room["code"])
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


def _pick_labels(room: dict, name: str, several: set = None) -> list:
    """One person's sent picks as display strings, e.g. "Masala Dosa ×2" or "Naan (sharing)"."""
    if several is None:
        several = {item["name"] for item in room["items"] if item["quantity"] > 1}
    counts = room["portions"].get(name, {})
    shown = []
    for dish in room["selections"].get(name, []):
        if dish not in several:
            shown.append(dish)
        elif counts.get(dish, 0) == 0:
            shown.append(f"{dish} (sharing)")
        else:
            shown.append(f"{dish} ×{counts[dish]}")
    return shown


def _pick_rows(room: dict, name: str) -> list:
    """One person's sent picks as rows for the waiting page, in the order the bill lists them.

    `portions` is None for a single dish, 0 for a several-of dish they share, or how many
    of them they had to themselves.
    """
    chosen = set(room["selections"].get(name, []))
    counts = room["portions"].get(name, {})
    return [
        {
            "id": item["id"],
            "name": item["name"],
            "category": item["category"],
            "quantity": item["quantity"],
            "portions": counts.get(item["name"], 0) if item["quantity"] > 1 else None,
        }
        for item in room["items"]
        if item["name"] in chosen
    ]


def _picks_for_host(room: dict) -> dict:
    """Each person's sent picks, for the host's Manage sheet."""
    several = {item["name"] for item in room["items"] if item["quantity"] > 1}
    return {name: _pick_labels(room, name, several) for name in room["selections"]}


@bp.route("/room/<room_code>/status")
def room_status(room_code):
    room = load_room(room_code)
    me = caller(room)
    # Every phone in a room asks every 3 seconds: sampled (see count_sampled).
    stage = "split" if room["finalized"] else "viewer" if not me else "waiting" if me["submitted"] else "picking"
    count_sampled("room.poll", stage=stage)
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
        # The caller's own sent picks (None until they have sent), so the waiting
        # page can show what they sent without anyone else's picks leaking.
        "my_picks": _pick_rows(room, me["name"]) if me and me["name"] in room["submitted_users"] else None,
        # Dishes where the portions sent don't add up to the bill's quantity.
        "quantity_mismatches": room["quantity_mismatches"] if me and me["is_host"] else [],
        # For dishes the bill lists several of: what each *other* person has sent
        # (units claimed, or 0 for sharing), so someone picking can see what is left.
        "taken": {
            str(item_id): [h for h in holders if h["name"] != me["name"]]
            for item_id, holders in room["taken"].items()
        } if me else {},
    })
