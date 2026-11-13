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
    c = new_client()
    c.get("/create")
    codes = [_scan(c, _jpeg_bytes()).status_code for _ in range(13)]
    assert codes[:12] == [200] * 12 and codes[12] == 429


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
    for _ in range(5):
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
