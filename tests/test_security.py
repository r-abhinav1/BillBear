"""Endpoint security: who may call what."""
import importlib
import io

import pytest
from itsdangerous import URLSafeTimedSerializer

from tests.conftest import join, make_room, needs_db, new_client, post_json, room, submit

pytestmark = needs_db


def _guest_room():
    host = new_client()
    code = make_room(host, num_people=3)
    bob = new_client()
    join(bob, code, "Bob")
    return code, host, bob


def test_submit_without_cookie_is_rejected():
    code, _host, _bob = _guest_room()
    stranger = new_client()
    assert submit(stranger, code, "Bob", ["Pizza"]).status_code == 403
    assert "Bob" not in room(code)["submitted_users"]


def test_cannot_submit_as_someone_else():
    code, _host, bob = _guest_room()
    resp = submit(bob, code, "Alice", ["Pizza"])
    assert resp.status_code == 403
    assert "Alice" not in room(code)["submitted_users"]


def test_cookie_name_spoofing_is_ignored():
    """A validly-signed cookie only proves the uid; the name inside it is not trusted."""
    from utils.session import make_session_token

    code, _host, bob = _guest_room()
    stranger = new_client()
    stranger.set_cookie(f"bb_session_{code}", make_session_token(code, "made-up-uid", "Alice"))
    assert post_json(stranger, f"/room/{code}/finalize").status_code == 403
    assert stranger.get(f"/room/{code}/status").get_json()["caller_name"] is None


def test_forged_cookie_with_default_key_rejected():
    code, _host, _bob = _guest_room()
    forged = URLSafeTimedSerializer("dev-secret-change-in-production", salt="bb-session-v1").dumps(
        {"rc": code, "uid": "x", "name": "Alice"}
    )
    c = new_client()
    c.set_cookie(f"bb_session_{code}", forged)
    assert c.get(f"/room/{code}/status").get_json()["caller_name"] is None


@pytest.mark.parametrize("path,body", [
    ("/host/remove-user", {"target_user": "Bob"}),
    ("/host/reset-device", {"target_user": "Bob"}),
    ("/host/num-people", {"num_people": 5}),
    ("/host/reopen-selections", {}),
    ("/host/revoke/Bob", {}),
    ("/finalize", {}),
])
def test_host_routes_require_host(path, body):
    code, _host, bob = _guest_room()
    assert post_json(bob, f"/room/{code}{path}", body).status_code == 403
    assert post_json(new_client(), f"/room/{code}{path}", body).status_code == 403


def test_edit_requires_host():
    code, _host, bob = _guest_room()
    assert bob.get(f"/edit/{code}").status_code == 403
    resp = bob.post(f"/edit/{code}", data={"item_id": [""], "item_name": ["Free"], "item_price": ["0"]})
    assert resp.status_code == 403
    assert [i["name"] for i in room(code)["items"]] == ["Pasta", "Pizza", "Coke"]


def test_guest_cannot_revoke_someone_else():
    code, host, bob = _guest_room()
    submit(host, code, "Alice", ["Pasta"])
    assert post_json(bob, f"/room/{code}/user/Alice/revoke").status_code == 403
    assert "Alice" in room(code)["submitted_users"]


@pytest.mark.parametrize("raw", ["[" * 40000 + "]" * 40000, '{"a":' * 30000 + "1" + "}" * 30000])
def test_deeply_nested_receipt_json_is_a_400_not_a_crash(raw):
    # json.loads raises RecursionError, not JSONDecodeError, past Python's nesting limit.
    resp = new_client().post("/create", data={
        "host_name": "Alice", "room_name": "x", "num_people": "2", "receipt_json": raw,
    })
    assert resp.status_code == 400
    assert b"corrupted" in resp.data


def test_cross_origin_post_blocked():
    code, _host, bob = _guest_room()
    resp = bob.post(
        f"/room/{code}/user/Bob/select-items",
        data={"selected_items": []},
        headers={"Origin": "https://evil.example", "X-Requested-With": "fetch"},
    )
    assert resp.status_code == 403
    resp = bob.post(
        f"/room/{code}/user/Bob/revoke",
        headers={"Referer": "https://evil.example/x", "X-Requested-With": "fetch"},
    )
    assert resp.status_code == 403
    ok = bob.post(
        f"/room/{code}/user/Bob/select-items",
        data={"selected_items": []},
        headers={"Origin": "http://localhost", "X-Requested-With": "fetch"},
    )
    assert ok.status_code == 200


def test_results_and_pdf_need_membership_and_finalize():
    host = new_client()
    code = make_room(host, num_people=1)
    submit(host, code, "Alice", ["Pasta"])
    post_json(host, f"/room/{code}/finalize")
    stranger = new_client()
    assert stranger.get(f"/room/{code}/results").status_code == 403
    assert stranger.get(f"/room/{code}/download").status_code == 403


def test_cron_auth():
    c = new_client()
    assert c.get("/api/cron/cleanup").status_code == 401
    assert c.get("/api/cron/cleanup", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/api/cron/cleanup", headers={"Authorization": "Bearer test-cron-secret"}).status_code == 200


def test_cron_fails_closed_without_secret(monkeypatch):
    monkeypatch.delenv("CRON_SECRET")
    assert new_client().get("/api/cron/cleanup", headers={"Authorization": "Bearer "}).status_code == 503


def test_room_code_guessing_is_rate_limited():
    c = new_client()
    statuses = [c.get("/room/ZZZZZZ/status").status_code for _ in range(32)]
    assert statuses[:30] == [404] * 30
    assert statuses[-1] == 429
    # Even a real room is hidden once the IP is blocked
    code = make_room(new_client())
    assert c.get(f"/room/{code}/status").status_code == 429


def test_security_headers_present(client):
    resp = client.get("/")
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "same-origin"
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]


def test_no_cors_headers_on_api(client):
    resp = client.get("/api/room/ABCDEF/my-status", headers={"Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in resp.headers


def test_session_cookie_flags(client):
    resp = client.post("/create", data={
        "host_name": "Alice", "room_name": "x", "num_people": "2", "skip_ocr": "true",
    })
    cookies = [v for k, v in resp.headers.items() if k == "Set-Cookie" and v.startswith("bb_session_")]
    assert cookies and "HttpOnly" in cookies[0] and "SameSite=Lax" in cookies[0]


# ---------------------------------------------------------------------------
# Scan endpoint
# ---------------------------------------------------------------------------

def _jpeg_bytes(w=40, h=80):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, format="JPEG")
    return buf.getvalue()


def _scan(c, data=b"", headers=None):
    return c.post(
        "/api/receipt/scan",
        data={"image": (io.BytesIO(data), "r.jpg")},
        content_type="multipart/form-data",
        headers=headers or {},
    )


def test_scan_requires_ticket(monkeypatch):
    c = new_client()
    assert _scan(c, _jpeg_bytes()).status_code == 403


def test_scan_success_with_ticket(monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "extract_receipt_from_vision", lambda b64, mime: {
        "items": [{"name": "Tea <script>", "price": "₹30"}], "total": "₹30.00",
    })
    c = new_client()
    c.get("/create")  # issues the scan ticket
    resp = _scan(c, _jpeg_bytes())
    assert resp.status_code == 200, resp.get_json()
    receipt = resp.get_json()["receipt"]
    assert receipt["items"] == [
        {"name": "Tea <script>", "price": 30.0, "category": "other", "quantity": 1}
    ]
    # No currency clue at all: rupees is only a guess, so the host is asked.
    assert receipt["currency"]["code"] == "INR" and receipt["currency"]["confirmed"] is False


def test_a_scan_with_more_dishes_than_a_bill_can_hold_keeps_the_first_200(monkeypatch):
    # It used to be a 500 after the paid model call had already been made.
    import app as app_module
    from utils.validation import MAX_ITEMS

    monkeypatch.setattr(app_module, "extract_receipt_from_vision", lambda b64, mime: {
        "items": [{"name": f"Line {n}", "price": "10"} for n in range(MAX_ITEMS + 50)],
    })
    c = new_client()
    c.get("/create")
    resp = _scan(c, _jpeg_bytes())
    assert resp.status_code == 200, resp.get_json()
    receipt = resp.get_json()["receipt"]
    assert len(receipt["items"]) == MAX_ITEMS and receipt["items"][-1]["name"] == f"Line {MAX_ITEMS - 1}"
    assert receipt["dropped"] == 50


def test_scan_cannot_confirm_its_own_currency(monkeypatch):
    """Text on the photo could make the model claim the currency is settled; only the host's tap counts."""
    import app as app_module

    monkeypatch.setattr(app_module, "extract_receipt_from_vision", lambda b64, mime: {
        "items": [{"name": "Ramen", "price": 1200}],
        "currency": {"code": "JPY", "confirmed": True, "evidence": ["symbol"]},
    })
    c = new_client()
    c.get("/create")
    currency = _scan(c, _jpeg_bytes()).get_json()["receipt"]["currency"]
    assert currency["code"] == "JPY" and currency["confirmed"] is False


def test_scan_rejects_non_image():
    c = new_client()
    c.get("/create")
    resp = _scan(c, b"%PDF-1.4 not an image")
    assert resp.status_code == 400


def test_scan_rejects_oversize():
    c = new_client()
    c.get("/create")
    resp = _scan(c, b"\xff" * (6 * 1024 * 1024))
    assert resp.status_code == 413


def test_scan_vision_failure_is_502(monkeypatch):
    import app as app_module
    from utils.receipt_pipeline import VisionFailed

    def boom(b64, mime):
        raise VisionFailed("nope")

    monkeypatch.setattr(app_module, "extract_receipt_from_vision", boom)
    c = new_client()
    c.get("/create")
    resp = _scan(c, _jpeg_bytes())
    assert resp.status_code == 502 and "manually" in resp.get_json()["message"]


def test_scan_rate_limited(monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "extract_receipt_from_vision", lambda b64, mime: {"items": [{"name": "A", "price": "1"}]})
    from utils.security import RATE_LIMITS

    limit = RATE_LIMITS["scan"][0]
    c = new_client()
    c.get("/create")
    codes = [_scan(c, _jpeg_bytes()).status_code for _ in range(limit + 1)]
    assert codes[:limit] == [200] * limit and codes[limit] == 429


def test_one_network_gets_a_small_share_of_the_days_scans():
    # One IP must not be able to use up the site's whole daily allowance.
    from utils.security import RATE_LIMITS

    assert RATE_LIMITS["scan"][0] <= RATE_LIMITS["scan_day"][0] <= 10


def test_secret_key_fails_closed_on_vercel(monkeypatch):
    import utils.session as session_module

    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("SECRET_KEY", "dev-secret-change-in-production")
    with pytest.raises(RuntimeError):
        session_module.get_secret_key()
    monkeypatch.delenv("SECRET_KEY")
    with pytest.raises(RuntimeError):
        session_module.get_secret_key()
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    assert session_module.get_secret_key() == "x" * 40
    importlib.reload(session_module)


def test_cron_writes_heartbeat_each_run():
    c = new_client()
    auth = {"Authorization": "Bearer test-cron-secret"}
    first = c.get("/api/cron/cleanup", headers=auth).get_json()
    second = c.get("/api/cron/cleanup", headers=auth).get_json()
    assert second["heartbeat_count"] == first["heartbeat_count"] + 1
    assert second["heartbeat_at"] >= first["heartbeat_at"]


def test_scan_global_daily_cap(monkeypatch):
    import app as app_module

    monkeypatch.setenv("SCAN_DAILY_CAP", "2")
    monkeypatch.setattr(app_module, "extract_receipt_from_vision", lambda b64, mime: {"items": [{"name": "A", "price": "1"}]})
    a, b = new_client(), new_client()
    a.get("/create")
    b.get("/create")
    assert _scan(a, _jpeg_bytes()).status_code == 200
    assert _scan(b, _jpeg_bytes()).status_code == 200
    capped = _scan(a, _jpeg_bytes())
    assert capped.status_code == 429 and capped.get_json()["code"] == "DAILY_CAP"


def test_proxy_https_origin_accepted_when_trusting_proxy(monkeypatch):
    """Behind ngrok/Vercel the browser Origin is https while Flask sees http."""
    from werkzeug.middleware.proxy_fix import ProxyFix

    import app as app_module

    code, _host, bob = _guest_room()
    original = app_module.app.wsgi_app
    app_module.app.wsgi_app = ProxyFix(original, x_for=1, x_proto=1, x_host=1)
    try:
        ok = bob.post(
            f"/room/{code}/user/Bob/select-items",
            data={"selected_items": []},
            headers={
                "Origin": "https://abc.ngrok-free.app",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "abc.ngrok-free.app",
                "X-Requested-With": "fetch",
            },
        )
        assert ok.status_code == 200
    finally:
        app_module.app.wsgi_app = original


# ---------------------------------------------------------------------------
# Abuse limits: who gets blocked, and who must not be
# ---------------------------------------------------------------------------

def test_junk_uploads_do_not_use_up_the_global_scan_cap(monkeypatch):
    """Only a real image headed for the paid vision API may spend a slot of the daily allowance."""
    import app as app_module

    monkeypatch.setenv("SCAN_DAILY_CAP", "2")
    calls = []
    monkeypatch.setattr(
        app_module, "extract_receipt_from_vision",
        lambda b64, mime: calls.append(1) or {"items": [{"name": "A", "price": "1"}]},
    )
    c = new_client()
    c.get("/create")
    for _ in range(2):      # junk still counts toward this network's own limit (5 per 10 min)
        assert _scan(c, b"%PDF-1.4 not an image").status_code == 400
    assert _scan(c, _jpeg_bytes()).status_code == 200
    assert _scan(c, _jpeg_bytes()).status_code == 200
    assert _scan(c, _jpeg_bytes()).get_json()["code"] == "DAILY_CAP"
    assert len(calls) == 2


def test_exhausted_day_turns_callers_away_before_decoding(monkeypatch):
    import app as app_module

    monkeypatch.setenv("SCAN_DAILY_CAP", "1")
    monkeypatch.setattr(app_module, "extract_receipt_from_vision", lambda b64, mime: {"items": [{"name": "A", "price": "1"}]})
    c = new_client()
    c.get("/create")
    assert _scan(c, _jpeg_bytes()).status_code == 200

    def must_not_run(raw):
        raise AssertionError("image was decoded after the daily cap was reached")

    monkeypatch.setattr(app_module, "prepare_receipt_image", must_not_run)
    resp = _scan(c, _jpeg_bytes())
    assert resp.status_code == 429 and resp.get_json()["code"] == "DAILY_CAP"


def test_room_members_are_not_locked_out_by_someone_elses_bad_guesses():
    """Shared Wi-Fi: a noisy stranger must not stop people from using the room they joined."""
    code, host, bob = _guest_room()
    noisy = new_client()
    for i in range(31):
        noisy.get(f"/room/QQQQ{i:02d}/status")
    assert noisy.get(f"/room/{code}/status").status_code == 429          # strangers stay blocked
    assert new_client().get(f"/room/{code}/status").status_code == 429   # including a fresh, cookie-less browser
    assert bob.get(f"/room/{code}/status").status_code == 200            # members keep working
    assert submit(bob, code, "Bob", ["Pizza"]).status_code == 200
    assert post_json(host, f"/room/{code}/host/num-people", {"num_people": 4}).status_code == 200


def test_stale_tab_on_an_expired_room_does_not_lock_the_network():
    from utils.session import make_session_token

    stale = new_client()
    stale.set_cookie("bb_session_ABCDEF", make_session_token("ABCDEF", "uid-1", "Bob"))
    assert [stale.get("/room/ABCDEF/status").status_code for _ in range(40)] == [404] * 40
    code = make_room(new_client())
    assert new_client().get(f"/room/{code}/status").status_code == 200


def test_host_token_guessing_stays_throttled_even_for_a_member():
    code, _host, bob = _guest_room()
    statuses = [bob.post(f"/room/{code}/host", data={"k": f"wrong-{i}"}).status_code for i in range(32)]
    assert statuses[:30] == [403] * 30
    assert statuses[-1] == 429


def test_pdf_download_is_throttled(monkeypatch):
    import app as app_module

    host = new_client()
    code = make_room(host, num_people=1)
    submit(host, code, "Alice", ["Pasta"])
    post_json(host, f"/room/{code}/finalize")
    monkeypatch.setattr(app_module, "PDF_AVAILABLE", True)
    monkeypatch.setattr(app_module, "render_pdf", lambda room, c: b"%PDF")
    statuses = [host.get(f"/room/{code}/download").status_code for _ in range(21)]
    assert statuses[:20] == [200] * 20
    assert statuses[20] == 429


# ---------------------------------------------------------------------------
# Bot check: a scan's token also covers Create, once
# ---------------------------------------------------------------------------
# A Turnstile token works once, and the next one takes seconds to arrive, so a
# quick tap on Create after a scan used to be refused as a "bot". The server now
# remembers a scan's accepted token for 15 minutes (cookie `bb_human`, sent only
# to /create) and Create takes that in place of a second token.

def _bot_check_on(monkeypatch, good=("good",)):
    """Turnstile switched on, with a stand-in Cloudflare that accepts only the `good` tokens."""
    import utils.security as security

    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "test-secret")
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "test-site")
    asked = []

    def post(url, data=None, timeout=None):
        asked.append(data["response"])
        accepted = data["response"] in good

        class Reply:
            ok = True
            status_code = 200

            def json(self):
                return {"success": True} if accepted else {"success": False, "error-codes": ["invalid-input-response"]}

        return Reply()

    monkeypatch.setattr(security.requests, "post", post)
    return asked


def _stub_vision(monkeypatch):
    import app as app_module

    monkeypatch.setattr(app_module, "extract_receipt_from_vision", lambda b64, mime: {
        "items": [{"name": "Tea", "price": "₹30"}], "total": "₹30.00",
    })


def _scan_with_token(c, token):
    return c.post(
        "/api/receipt/scan",
        data={"image": (io.BytesIO(_jpeg_bytes()), "r.jpg"), "cf-turnstile-response": token},
        content_type="multipart/form-data",
    )


def _create_form(**extra):
    import json

    receipt = {"items": [{"name": "Tea", "price": "30"}], "subtotal": "30", "total": "30",
               "extraction_meta": {"source": "vision_llm"}}
    data = {"host_name": "Alice", "room_name": "Dinner", "num_people": "2", "receipt_json": json.dumps(receipt)}
    data.update(extra)
    return data


def test_a_scan_the_bot_check_accepted_lets_create_skip_a_second_token(monkeypatch):
    asked = _bot_check_on(monkeypatch)
    _stub_vision(monkeypatch)
    c = new_client()
    c.get("/create")
    scan = _scan_with_token(c, "good")
    assert scan.status_code == 200, scan.get_json()
    cookie = next(h for h in scan.headers.getlist("Set-Cookie") if h.startswith("bb_human="))
    assert "Path=/create" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    # The page's next token has not arrived yet: Create sends none, and still goes through.
    resp = c.post("/create", data=_create_form())
    assert resp.status_code == 302, resp.get_data(as_text=True)[:300]
    assert asked == ["good"]          # Cloudflare was asked once, for the scan


def test_create_without_a_token_or_a_pass_is_refused_and_says_why(monkeypatch, caplog):
    asked = _bot_check_on(monkeypatch)
    c = new_client()
    c.get("/create")
    with caplog.at_level("WARNING", logger="billbear.security"):
        resp = c.post("/create", data=_create_form())
    assert resp.status_code == 403
    assert "Bot check failed" in resp.get_data(as_text=True)
    assert asked == []                # nothing to check, so Cloudflare was not called
    assert "no token sent (create)" in caplog.text


def test_create_with_its_own_fresh_token_still_works(monkeypatch):
    """The manual path never scans: its token comes from the page load."""
    asked = _bot_check_on(monkeypatch)
    c = new_client()
    c.get("/create")
    assert c.post("/create", data=_create_form(**{"cf-turnstile-response": "good"})).status_code == 302
    assert asked == ["good"]


def test_a_refused_token_is_logged_with_cloudflares_reason_but_not_the_token(monkeypatch, caplog):
    _bot_check_on(monkeypatch)
    c = new_client()
    c.get("/create")
    with caplog.at_level("WARNING", logger="billbear.security"):
        resp = c.post("/create", data=_create_form(**{"cf-turnstile-response": "secret-looking-token"}))
    assert resp.status_code == 403
    assert "invalid-input-response" in caplog.text
    assert "secret-looking-token" not in caplog.text


def test_the_pass_covers_one_room(monkeypatch):
    _bot_check_on(monkeypatch)
    _stub_vision(monkeypatch)
    c = new_client()
    c.get("/create")
    assert _scan_with_token(c, "good").status_code == 200
    assert c.post("/create", data=_create_form()).status_code == 302
    assert c.post("/create", data=_create_form()).status_code == 403     # used up


def test_a_failed_scan_check_gives_no_pass(monkeypatch):
    _bot_check_on(monkeypatch)
    _stub_vision(monkeypatch)
    c = new_client()
    c.get("/create")
    assert _scan_with_token(c, "bad").status_code == 403
    assert c.post("/create", data=_create_form()).status_code == 403


def test_the_pass_does_not_skip_the_check_on_scans(monkeypatch):
    """Scans are paid model calls: each needs its own token, pass or not."""
    _bot_check_on(monkeypatch)
    _stub_vision(monkeypatch)
    c = new_client()
    c.get("/create")
    assert _scan_with_token(c, "good").status_code == 200
    assert _scan_with_token(c, "").status_code == 403


def test_a_forged_or_stale_pass_is_ignored(monkeypatch):
    _bot_check_on(monkeypatch)
    c = new_client()
    c.get("/create")
    c.set_cookie("bb_human", "not-a-signed-value", path="/create")
    assert c.post("/create", data=_create_form()).status_code == 403
    # A real pass from another purpose's key (the scan ticket's) is no good either.
    scan_ticket = URLSafeTimedSerializer("test-secret-key-for-pytest-only-0123456789", salt="bb-scan-v1").dumps({"n": "x"})
    c.set_cookie("bb_human", scan_ticket, path="/create")
    assert c.post("/create", data=_create_form()).status_code == 403


def test_with_the_bot_check_off_nothing_changes():
    c = new_client()
    assert c.post("/create", data=_create_form()).status_code == 302
    assert not any(h.startswith("bb_human=") for h in c.get("/create").headers.getlist("Set-Cookie"))
