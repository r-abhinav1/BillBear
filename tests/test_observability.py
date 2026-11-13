"""Sentry scrubbing, the load-test pass and the health check."""
import pytest

from tests.conftest import app, join, make_room, needs_db, new_client
from utils.observability import _scrub, capture, cron_monitor, enabled
from utils.security import LOAD_TEST_HEADER


def test_events_carry_no_names_bodies_cookies_or_ips():
    event = {
        "request": {
            "url": "http://x/room/AB12CD/user/Asha/select-items?k=secret",
            "query_string": "k=secret",
            "data": {"user_name": "Asha", "receipt_json": "{...}"},
            "cookies": {"session": "s"},
            "env": {"REMOTE_ADDR": "1.2.3.4"},
            "headers": {"User-Agent": "phone", "Cookie": "session=s", "X-Load-Test": "t", "X-Forwarded-For": "1.2.3.4"},
        },
        "user": {"ip_address": "1.2.3.4"},
    }
    with app.test_request_context("/room/AB12CD/user/Asha/select-items?k=secret"):
        out = _scrub(event)
    assert out["request"]["url"] == "http://localhost/room/<room_code>/user/<user_name>/select-items"
    assert out["request"]["headers"] == {"User-Agent": "phone"}
    for gone in ("query_string", "data", "cookies", "env"):
        assert gone not in out["request"]
    assert "user" not in out


def test_sentry_is_off_without_a_dsn():
    assert not enabled()
    capture(RuntimeError("nothing is sent"))          # no-op, no error
    with cron_monitor():
        pass


@needs_db
def test_healthz_reports_the_database(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200 and resp.get_json() == {"status": "ok", "database": "ok"}
    assert resp.headers["Cache-Control"] == "no-store"


@needs_db
def test_load_test_pass_skips_network_limits_only_with_the_secret(monkeypatch):
    import utils.security as security

    monkeypatch.setitem(security.RATE_LIMITS, "join", (1, 600))
    code = make_room(new_client(), num_people=10)
    token = "k6-" + "x" * 40
    header = {LOAD_TEST_HEADER: token}

    # Without the secret on the server, the header means nothing.
    assert join(new_client(), code, "Ben").status_code == 302
    assert new_client().post(f"/join/{code}", data={"user_name": "Cara"}, headers=header).status_code == 429

    monkeypatch.setenv("LOAD_TEST_TOKEN", token)
    assert new_client().post(f"/join/{code}", data={"user_name": "Cara"}, headers=header).status_code == 302
    assert new_client().post(f"/join/{code}", data={"user_name": "Dev"},
                             headers={LOAD_TEST_HEADER: "wrong" + token}).status_code == 429
    monkeypatch.setenv("LOAD_TEST_TOKEN", "short")               # too short to count as a secret
    assert new_client().post(f"/join/{code}", data={"user_name": "Esha"}, headers={LOAD_TEST_HEADER: "short"}).status_code == 429


@needs_db
def test_load_test_pass_never_lifts_scan_limits(monkeypatch):
    import utils.security as security
    from tests.test_security import _jpeg_bytes, _scan

    token = "k6-" + "x" * 40
    monkeypatch.setenv("LOAD_TEST_TOKEN", token)
    monkeypatch.setitem(security.RATE_LIMITS, "scan", (0, 600))
    c = new_client()
    c.get("/create")
    resp = c.post("/api/receipt/scan", data={"image": (__import__("io").BytesIO(_jpeg_bytes()), "r.jpg")},
                  headers={LOAD_TEST_HEADER: token}, content_type="multipart/form-data")
    assert resp.status_code == 429


def test_metrics_do_nothing_without_sentry():
    from utils.observability import count, count_sampled, distribution, distribution_sampled

    count("scan.result", outcome="success")
    distribution("pdf.render", 12.0, "millisecond")
    count_sampled("room.poll", stage="picking")
    distribution_sampled("db.connect", 3.0, "millisecond")


def test_sampled_counts_are_scaled_so_totals_stay_right(monkeypatch):
    import utils.observability as obs

    sent = []
    monkeypatch.setattr(obs, "_enabled", True)
    monkeypatch.setattr(obs, "count", lambda name, value=1, **a: sent.append((name, value, a)))
    monkeypatch.setattr(obs, "_HIGH_VOLUME_RATE", 0.1)
    monkeypatch.setattr("random.random", lambda: 0.05)          # chosen
    obs.count_sampled("room.poll", stage="waiting")
    monkeypatch.setattr("random.random", lambda: 0.5)           # not chosen
    obs.count_sampled("room.poll", stage="waiting")
    assert sent == [("room.poll", 10.0, {"stage": "waiting"})]


def test_rare_metrics_flush_at_the_end_of_their_request(monkeypatch):
    import sentry_sdk

    import utils.observability as obs
    from tests.conftest import app

    flushed = []
    monkeypatch.setattr(obs, "_enabled", True)
    monkeypatch.setattr(sentry_sdk.metrics, "count", lambda *a, **k: None)
    monkeypatch.setattr(sentry_sdk, "flush", lambda timeout=None: flushed.append(timeout))
    with app.test_request_context("/"):
        from flask import g

        g.load_test = True
        assert obs._attrs("room.poll", {"stage": "picking"}) == {"stage": "picking", "load_test": "true"}
        obs.count("room.poll", stage="picking")
        obs.flush_if_marked()
        assert flushed == []                          # polls never wait
        obs.count("bill.split", people=3)
        obs.flush_if_marked()
        assert flushed == [1]
