"""
Integration tests for the session-resilience + host-controls features.

Run with: .venv/bin/python -m pytest tests/test_session_resilience.py -v
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Ensure we use in-memory storage and deterministic secrets for tests.
os.environ.setdefault("SECRET_KEY", "test-secret-for-tests-only")
os.environ.setdefault("OCR_TEXT_INGESTION_ENABLED", "true")
os.environ.setdefault("LEGACY_IMAGE_OCR_ENABLED", "false")
os.environ.setdefault("CLIENT_OCR_ENABLED", "true")

import storage as storage_module  # noqa: E402
from app import app, ensure_room_shape  # noqa: E402
from utils.bill_audit import audit_bill  # noqa: E402
from utils.bill_split import calculate_bill_split  # noqa: E402
from utils.session import make_session_token, issue_user_id  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_fallback_rooms():
    storage_module._fallback_rooms.clear()
    yield
    storage_module._fallback_rooms.clear()


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _make_room(code="TESTRM", host="Alice", items=None, guests=()):
    """Create a fully-populated test room with the new schema."""
    items = items or [
        {"name": "Pasta", "price": "₹200.00"},
        {"name": "Pizza", "price": "₹400.00"},
        {"name": "Coke", "price": "₹100.00"},
    ]
    host_uid = issue_user_id()
    user_ids = {host: host_uid}
    users = [host]
    for g in guests:
        users.append(g)
        user_ids[g] = issue_user_id()
    room = {
        "host_name": host,
        "room_name": "Dinner",
        "num_people": max(len(users), 2),
        "items": items,
        "ocr_data": {
            "subtotal": "₹700.00",
            "serviceCharge": "N/A",
            "discount": "N/A",
            "cgst": "N/A",
            "sgst": "N/A",
            "igst": "N/A",
            "total": "₹700.00",
        },
        "users": users,
        "selections": {},
        "submitted_users": set(),
        "user_ids": user_ids,
        "host_user_id": host_uid,
        "submission_ids": {},
        "last_selections": {},
        "schema_version": 2,
    }
    storage_module.save_room(code, room)
    return room, user_ids


def _set_cookie_for(client, room_code, user_name, uid):
    token = make_session_token(room_code, uid, user_name)
    client.set_cookie(
        f"bb_session_{room_code.upper()}",
        token,
    )


# ---------------------------------------------------------------------------
# Bill-split math
# ---------------------------------------------------------------------------

def test_bill_split_recomputes_when_user_removed():
    room = {
        "items": [
            {"name": "Pasta", "price": "₹200.00"},
            {"name": "Pizza", "price": "₹400.00"},
        ],
        "ocr_data": {"subtotal": "₹600.00", "total": "₹600.00"},
        "selections": {"A": ["Pasta"], "B": ["Pasta", "Pizza"], "C": ["Pizza"]},
    }
    r3 = calculate_bill_split(room)
    # Pizza shared A+B+C? No — only B and C. Pasta shared A+B.
    # A: 200/2 = 100; B: 200/2 + 400/2 = 300; C: 400/2 = 200; total 600
    assert r3["user_breakdown"]["A"]["item_total"] == 100.0
    assert r3["user_breakdown"]["B"]["item_total"] == 300.0
    assert r3["user_breakdown"]["C"]["item_total"] == 200.0

    # Remove C (duplicate rejoin scenario)
    room["selections"].pop("C")
    r2 = calculate_bill_split(room)
    # Pasta still A+B (100 each). Pizza now only B (400).
    assert r2["user_breakdown"]["A"]["item_total"] == 100.0
    assert r2["user_breakdown"]["B"]["item_total"] == 500.0
    assert "C" not in r2["user_breakdown"]


def test_tax_service_split_only_among_paying_users():
    """A user with an empty selection doesn't owe any tax/service share."""
    room = {
        "items": [{"name": "Pasta", "price": "₹200.00"}],
        "ocr_data": {
            "subtotal": "₹200.00",
            "serviceCharge": "₹20.00",
            "cgst": "₹10.00",
            "sgst": "₹10.00",
            "total": "₹240.00",
        },
        # Empty selections (e.g. force-completed)
        "selections": {"A": ["Pasta"], "B": [], "C": []},
    }
    r = calculate_bill_split(room)
    # B and C picked nothing → owe ₹0
    assert r["user_breakdown"]["B"]["final_amount"] == 0.0
    assert r["user_breakdown"]["C"]["final_amount"] == 0.0
    assert r["user_breakdown"]["B"]["cgst"] == 0.0
    assert r["user_breakdown"]["C"]["service_charge"] == 0.0
    # A is the sole payer → eats all taxes/service
    a = r["user_breakdown"]["A"]
    assert a["item_total"] == 200.0
    assert a["service_charge"] == 20.0
    assert a["cgst"] == 10.0
    assert a["sgst"] == 10.0
    assert a["final_amount"] == 240.0
    # Grand total equals bill total
    assert r["totals"]["grand_total"] == 240.0


def test_math_reflects_selection_changes_end_to_end(client):
    """Submit → modify via revoke → re-submit → math tracks every step."""
    _, uids = _make_room(guests=["Bob", "Charlie"])
    room = storage_module.get_room("TESTRM")
    # Add tax+service so math is non-trivial
    room["ocr_data"]["serviceCharge"] = "₹70.00"
    room["ocr_data"]["cgst"] = "₹17.50"
    room["ocr_data"]["sgst"] = "₹17.50"
    room["ocr_data"]["total"] = "₹805.00"  # 700 + 70 + 35 = 805
    storage_module.save_room("TESTRM", room)

    # Alice + Bob + Charlie each pick different items; 3 paying users
    _set_cookie_for(client, "TESTRM", "Alice", uids["Alice"])
    client.post("/room/TESTRM/user/Alice/select-items",
                data={"selected_items": ["Pasta"], "submission_id": "a1", "ajax": "1"})
    client.delete_cookie("bb_session_TESTRM")
    _set_cookie_for(client, "TESTRM", "Bob", uids["Bob"])
    client.post("/room/TESTRM/user/Bob/select-items",
                data={"selected_items": ["Pizza"], "submission_id": "b1", "ajax": "1"})
    client.delete_cookie("bb_session_TESTRM")
    _set_cookie_for(client, "TESTRM", "Charlie", uids["Charlie"])
    client.post("/room/TESTRM/user/Charlie/select-items",
                data={"selected_items": ["Coke"], "submission_id": "c1", "ajax": "1"})

    room = storage_module.get_room("TESTRM")
    split1 = calculate_bill_split(room)
    # Three paying users, each owes items + (service+cgst+sgst)/3 = +35
    assert split1["user_breakdown"]["Alice"]["final_amount"] == round(200 + 35, 2)
    assert split1["user_breakdown"]["Bob"]["final_amount"]   == round(400 + 35, 2)
    assert split1["user_breakdown"]["Charlie"]["final_amount"] == round(100 + 35, 2)
    assert split1["totals"]["grand_total"] == 805.0

    # Bob revokes — split now over 2 paying users → tax/2 each
    client.delete_cookie("bb_session_TESTRM")
    _set_cookie_for(client, "TESTRM", "Bob", uids["Bob"])
    client.post("/room/TESTRM/user/Bob/revoke")
    room = storage_module.get_room("TESTRM")
    split2 = calculate_bill_split(room)
    assert "Bob" not in split2["user_breakdown"]
    assert split2["user_breakdown"]["Alice"]["final_amount"]   == round(200 + 52.5, 2)
    assert split2["user_breakdown"]["Charlie"]["final_amount"] == round(100 + 52.5, 2)
    # Bob's ₹400 pizza is unaccounted — grand total drops
    assert split2["totals"]["grand_total"] == round(200 + 100 + 105, 2)

    # Bob re-submits a NEW selection → math fully updated
    client.post("/room/TESTRM/user/Bob/select-items",
                data={"selected_items": ["Pizza", "Coke"], "submission_id": "b2", "ajax": "1"})
    room = storage_module.get_room("TESTRM")
    split3 = calculate_bill_split(room)
    # Coke is now shared by Bob+Charlie → 100/2 = 50 each
    assert split3["user_breakdown"]["Charlie"]["item_total"] == 50.0
    assert split3["user_breakdown"]["Bob"]["item_total"] == round(400 + 50, 2)
    assert split3["totals"]["grand_total"] == 805.0


def test_host_num_people_does_not_affect_split_math():
    """num_people is expected-headcount only; split uses submitted users."""
    room = {
        "items": [{"name": "A", "price": "₹100.00"}],
        "ocr_data": {"subtotal": "₹100.00", "cgst": "₹10.00", "total": "₹110.00"},
        "selections": {"A": ["A"], "B": ["A"]},
        "num_people": 10,  # host said 10, but only 2 submitted
    }
    r = calculate_bill_split(room)
    # Tax split /2 (paying users), not /10
    assert r["user_breakdown"]["A"]["cgst"] == 5.0
    assert r["user_breakdown"]["B"]["cgst"] == 5.0


def test_audit_flags_unassigned_items():
    room = {
        "items": [
            {"name": "Pasta", "price": "₹200.00"},
            {"name": "Pizza", "price": "₹400.00"},  # nobody picks
            {"name": "Coke", "price": "₹100.00"},
        ],
        "ocr_data": {"subtotal": "₹700.00", "total": "₹700.00"},
        "selections": {"A": ["Pasta"], "B": ["Coke"]},
    }
    split = calculate_bill_split(room)
    warnings = audit_bill(room, split)
    titles = [w["title"] for w in warnings]
    assert any("not picked" in t for t in titles)
    assert any("Items paid for < bill subtotal" in t or "Split total" in t for t in titles)


def test_audit_clean_when_everything_reconciles():
    room = {
        "items": [{"name": "A", "price": "₹100.00"}],
        "ocr_data": {"subtotal": "₹100.00", "total": "₹100.00"},
        "selections": {"X": ["A"]},
    }
    split = calculate_bill_split(room)
    warnings = audit_bill(room, split)
    assert len(warnings) == 1
    assert warnings[0]["severity"] == "ok"


# ---------------------------------------------------------------------------
# Idempotent submit
# ---------------------------------------------------------------------------

def test_idempotent_submit_same_id_is_no_op(client):
    _, uids = _make_room(guests=["Bob"])
    _set_cookie_for(client, "TESTRM", "Bob", uids["Bob"])

    sid = "test-sid-abc"
    # First submit persists
    resp = client.post(
        "/room/TESTRM/user/Bob/select-items",
        data={"selected_items": ["Pasta"], "submission_id": sid, "ajax": "1"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "success"
    assert resp.get_json()["idempotent"] is False

    room = storage_module.get_room("TESTRM")
    assert room["submission_ids"]["Bob"] == sid
    assert room["selections"]["Bob"] == ["Pasta"]

    # Replay with same submission_id but DIFFERENT items — should be a no-op,
    # original selection wins. This simulates a retry of a dropped response.
    resp2 = client.post(
        "/room/TESTRM/user/Bob/select-items",
        data={"selected_items": ["Pizza", "Coke"], "submission_id": sid, "ajax": "1"},
    )
    body = resp2.get_json()
    assert resp2.status_code == 200
    assert body["status"] == "success"
    assert body["idempotent"] is True

    room = storage_module.get_room("TESTRM")
    assert room["selections"]["Bob"] == ["Pasta"]  # unchanged


def test_new_submission_id_overwrites(client):
    _, uids = _make_room(guests=["Bob"])
    _set_cookie_for(client, "TESTRM", "Bob", uids["Bob"])

    client.post(
        "/room/TESTRM/user/Bob/select-items",
        data={"selected_items": ["Pasta"], "submission_id": "sid-1", "ajax": "1"},
    )
    # User revokes (or a legitimate edit) -> new submission_id
    client.post(
        "/room/TESTRM/user/Bob/select-items",
        data={"selected_items": ["Pizza"], "submission_id": "sid-2", "ajax": "1"},
    )
    room = storage_module.get_room("TESTRM")
    assert room["selections"]["Bob"] == ["Pizza"]
    assert room["submission_ids"]["Bob"] == "sid-2"


# ---------------------------------------------------------------------------
# my-status endpoint
# ---------------------------------------------------------------------------

def test_my_status_without_cookie(client):
    _make_room()
    resp = client.get("/api/room/TESTRM/my-status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["authenticated"] is False
    assert data["submitted"] is False


def test_my_status_reports_submitted_after_submit(client):
    _, uids = _make_room(guests=["Bob"])
    _set_cookie_for(client, "TESTRM", "Bob", uids["Bob"])

    sid = "my-sid"
    client.post(
        "/room/TESTRM/user/Bob/select-items",
        data={"selected_items": ["Pizza"], "submission_id": sid, "ajax": "1"},
    )

    resp = client.get("/api/room/TESTRM/my-status")
    data = resp.get_json()
    assert data["authenticated"] is True
    assert data["submitted"] is True
    assert data["submission_id"] == sid
    assert data["selections"] == ["Pizza"]
    assert "/waiting/" in data["redirect"]


# ---------------------------------------------------------------------------
# Self revoke
# ---------------------------------------------------------------------------

def test_self_revoke_allows_reedit(client):
    _, uids = _make_room(guests=["Bob"])
    _set_cookie_for(client, "TESTRM", "Bob", uids["Bob"])

    client.post(
        "/room/TESTRM/user/Bob/select-items",
        data={"selected_items": ["Pasta", "Coke"], "submission_id": "s1", "ajax": "1"},
    )

    resp = client.post("/room/TESTRM/user/Bob/revoke")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "success"
    assert "/select-items" in body["redirect"]

    room = storage_module.get_room("TESTRM")
    assert "Bob" not in room["submitted_users"]
    assert "Bob" not in room["selections"]
    assert room["last_selections"]["Bob"] == ["Pasta", "Coke"]  # preserved for pre-check

    # GET select-items now shows the page with pre-check hint in context.
    resp2 = client.get("/room/TESTRM/user/Bob/select-items")
    assert resp2.status_code == 200


def test_self_revoke_requires_matching_cookie(client):
    _, uids = _make_room(guests=["Bob"])
    _set_cookie_for(client, "TESTRM", "Bob", uids["Bob"])
    # Bob tries to revoke Alice
    resp = client.post("/room/TESTRM/user/Alice/revoke")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Host controls
# ---------------------------------------------------------------------------

def test_host_remove_user_purges_all_traces(client):
    _, uids = _make_room(guests=["Bob", "Charlie"])
    # Bob submits
    _set_cookie_for(client, "TESTRM", "Bob", uids["Bob"])
    client.post(
        "/room/TESTRM/user/Bob/select-items",
        data={"selected_items": ["Pizza"], "submission_id": "s-bob", "ajax": "1"},
    )
    # Switch to host session
    client.delete_cookie("bb_session_TESTRM")
    _set_cookie_for(client, "TESTRM", "Alice", uids["Alice"])

    resp = client.post(
        "/room/TESTRM/host/remove-user",
        json={"target_user": "Bob"},
    )
    assert resp.status_code == 200

    room = storage_module.get_room("TESTRM")
    assert "Bob" not in room["users"]
    assert "Bob" not in room["selections"]
    assert "Bob" not in room["user_ids"]
    assert "Bob" not in room["submission_ids"]
    assert "Bob" not in room["submitted_users"]


def test_host_remove_user_rejects_non_host(client):
    _, uids = _make_room(guests=["Bob"])
    _set_cookie_for(client, "TESTRM", "Bob", uids["Bob"])  # not host
    resp = client.post("/room/TESTRM/host/remove-user", json={"target_user": "Alice"})
    assert resp.status_code == 403


def test_host_cannot_remove_self(client):
    _, uids = _make_room(guests=["Bob"])
    _set_cookie_for(client, "TESTRM", "Alice", uids["Alice"])
    resp = client.post("/room/TESTRM/host/remove-user", json={"target_user": "Alice"})
    assert resp.status_code == 400


def test_host_revoke_user(client):
    _, uids = _make_room(guests=["Bob"])
    _set_cookie_for(client, "TESTRM", "Bob", uids["Bob"])
    client.post(
        "/room/TESTRM/user/Bob/select-items",
        data={"selected_items": ["Pasta"], "submission_id": "s1", "ajax": "1"},
    )

    client.delete_cookie("bb_session_TESTRM")
    _set_cookie_for(client, "TESTRM", "Alice", uids["Alice"])
    resp = client.post("/room/TESTRM/host/revoke/Bob")
    assert resp.status_code == 200

    room = storage_module.get_room("TESTRM")
    assert "Bob" not in room["submitted_users"]
    assert "Bob" not in room["selections"]
    assert room["last_selections"]["Bob"] == ["Pasta"]


def test_host_num_people_update(client):
    _, uids = _make_room(guests=["Bob"])
    _set_cookie_for(client, "TESTRM", "Alice", uids["Alice"])
    resp = client.post("/room/TESTRM/host/num-people", json={"num_people": 5})
    assert resp.status_code == 200
    room = storage_module.get_room("TESTRM")
    assert room["num_people"] == 5


def test_host_num_people_cannot_go_below_current(client):
    _, uids = _make_room(guests=["Bob", "Charlie"])  # 3 joined
    _set_cookie_for(client, "TESTRM", "Alice", uids["Alice"])
    resp = client.post("/room/TESTRM/host/num-people", json={"num_people": 1})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Host revert (reopen selections)
# ---------------------------------------------------------------------------

def test_host_revert_clears_submissions_and_preserves_picks(client):
    _, uids = _make_room(guests=["Bob", "Charlie"])
    # Both guests submit different things
    _set_cookie_for(client, "TESTRM", "Bob", uids["Bob"])
    client.post(
        "/room/TESTRM/user/Bob/select-items",
        data={"selected_items": ["Pasta", "Coke"], "submission_id": "b-1", "ajax": "1"},
    )
    client.delete_cookie("bb_session_TESTRM")
    _set_cookie_for(client, "TESTRM", "Charlie", uids["Charlie"])
    client.post(
        "/room/TESTRM/user/Charlie/select-items",
        data={"selected_items": ["Pizza"], "submission_id": "c-1", "ajax": "1"},
    )

    # Host reverts
    client.delete_cookie("bb_session_TESTRM")
    _set_cookie_for(client, "TESTRM", "Alice", uids["Alice"])
    resp = client.post("/room/TESTRM/host/reopen-selections", json={})
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "success"

    room = storage_module.get_room("TESTRM")
    assert room["submitted_users"] == set()
    assert room["selections"] == {}
    assert room["submission_ids"] == {}
    # prior picks stashed for pre-check
    assert room["last_selections"]["Bob"] == ["Pasta", "Coke"]
    assert room["last_selections"]["Charlie"] == ["Pizza"]


def test_host_revert_requires_host(client):
    _, uids = _make_room(guests=["Bob"])
    _set_cookie_for(client, "TESTRM", "Bob", uids["Bob"])
    resp = client.post("/room/TESTRM/host/reopen-selections", json={})
    assert resp.status_code == 403


def test_select_items_preselects_after_revert(client):
    _, uids = _make_room(guests=["Bob"])
    _set_cookie_for(client, "TESTRM", "Bob", uids["Bob"])
    client.post(
        "/room/TESTRM/user/Bob/select-items",
        data={"selected_items": ["Pizza"], "submission_id": "b-1", "ajax": "1"},
    )

    # Host reverts
    client.delete_cookie("bb_session_TESTRM")
    _set_cookie_for(client, "TESTRM", "Alice", uids["Alice"])
    client.post("/room/TESTRM/host/reopen-selections", json={})

    # Bob lands on select-items; Pizza should be pre-checked in the HTML.
    client.delete_cookie("bb_session_TESTRM")
    _set_cookie_for(client, "TESTRM", "Bob", uids["Bob"])
    resp = client.get("/room/TESTRM/user/Bob/select-items")
    body = resp.data.decode()
    # Pizza checkbox is followed by "checked"; Pasta is not.
    import re
    pizza_attrs = re.search(r'value="Pizza"[^>]*', body).group(0)
    pasta_attrs = re.search(r'value="Pasta"[^>]*', body).group(0)
    assert "checked" in pizza_attrs
    assert "checked" not in pasta_attrs


# ---------------------------------------------------------------------------
# Cookie reclaim on join
# ---------------------------------------------------------------------------

def test_join_with_valid_cookie_is_reclaim_not_dup(client):
    _, uids = _make_room(guests=["Bob"])
    _set_cookie_for(client, "TESTRM", "Bob", uids["Bob"])

    # Bob re-opens the join link. Without the cookie fix, this would show the
    # join form. With the cookie, they bypass straight to their current state.
    resp = client.get("/join/TESTRM", follow_redirects=False)
    assert resp.status_code == 302
    assert "/room/TESTRM/user/Bob" in resp.location

    # Verify no duplicate created
    room = storage_module.get_room("TESTRM")
    assert room["users"].count("Bob") == 1


def test_join_without_cookie_same_name_still_rejected(client):
    _make_room(guests=["Bob"])
    # No cookie set — this is a truly new browser
    resp = client.post(
        "/join/TESTRM",
        data={"user_name": "Bob"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert b"already joined" in resp.data


def test_new_join_issues_cookie_and_user_id(client):
    _make_room()  # just Alice the host, num_people=2
    resp = client.post(
        "/join/TESTRM",
        data={"user_name": "Bob"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    # Cookie should be set on this response
    cookies = [h for h in resp.headers.getlist("Set-Cookie") if h.startswith("bb_session_TESTRM=")]
    assert len(cookies) == 1

    room = storage_module.get_room("TESTRM")
    assert "Bob" in room["users"]
    assert "Bob" in room["user_ids"]


# ---------------------------------------------------------------------------
# ensure_room_shape defensive defaults
# ---------------------------------------------------------------------------

def test_ensure_room_shape_fills_missing_fields():
    legacy_room = {
        "host_name": "A", "room_name": "R", "num_people": 2,
        "items": [], "users": ["A"], "selections": {}, "submitted_users": []
    }
    ensure_room_shape(legacy_room)
    assert isinstance(legacy_room["submitted_users"], set)
    assert legacy_room["user_ids"] == {}
    assert legacy_room["submission_ids"] == {}
    assert legacy_room["last_selections"] == {}
    assert "schema_version" in legacy_room


# ---------------------------------------------------------------------------
# Saved JSON upload accepted by /create
# ---------------------------------------------------------------------------

def test_create_accepts_saved_scan_json(client):
    payload = {
        "_vision_receipt": {
            "items": [
                {"name": "Burger", "price": "150"},
                {"name": "Fries", "price": "90"},
            ],
            "subtotal": "240",
            "total": "240",
            "serviceCharge": "N/A",
            "discount": "N/A",
            "cgst": "N/A",
            "sgst": "N/A",
            "igst": "N/A",
            "restaurant_name": "N/A",
            "parser_meta": {"source": "user_saved_json", "used_fallback": False},
            "extraction_meta": {"mode": "saved_json", "source": "user_saved_json", "provider": "none"},
        }
    }
    resp = client.post(
        "/create",
        data={
            "host_name": "Alice",
            "room_name": "Lunch",
            "num_people": "2",
            "ocr_payload": json.dumps(payload),
        },
        content_type="multipart/form-data",
    )
    # On success the server renders edit_items.html inline (no redirect).
    assert resp.status_code == 200
    assert b"Edit Bill Items" in resp.data
    # And the set-cookie for the host session should be present
    cookies = resp.headers.getlist("Set-Cookie")
    assert any(c.startswith("bb_session_") for c in cookies)
