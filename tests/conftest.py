"""
Test setup. Integration tests need a throwaway Postgres in TEST_DATABASE_URL
(e.g. the local docker one from .env.example); they are skipped otherwise.
Every test starts from empty tables.
"""
import json
import os
import re
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
if TEST_DATABASE_URL:
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["SECRET_KEY"] = "test-secret-key-for-pytest-only-0123456789"
os.environ.pop("VERCEL", None)
os.environ.pop("TURNSTILE_SECRET_KEY", None)
os.environ.pop("TURNSTILE_SITE_KEY", None)
os.environ["CRON_SECRET"] = "test-cron-secret"
# Tests never report to Sentry, even with a DSN in .env (empty, so app.py's
# load_dotenv can't fill it back in).
os.environ["SENTRY_DSN"] = ""

import psycopg  # noqa: E402

from app import app  # noqa: E402

needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")

DEFAULT_ITEMS = [("Pasta", "200"), ("Pizza", "400"), ("Coke", "100")]


@pytest.fixture(scope="session", autouse=True)
def _migrated():
    if TEST_DATABASE_URL:
        from scripts.migrate import apply_migrations

        apply_migrations(TEST_DATABASE_URL, verbose=False)
    yield


@pytest.fixture(autouse=True)
def _clean_tables():
    if TEST_DATABASE_URL:
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
            conn.execute("TRUNCATE rooms, participants, items, claims, rate_limits RESTART IDENTITY CASCADE")
    yield


@pytest.fixture(autouse=True)
def _no_real_ngrok(monkeypatch):
    """Don't let a tunnel running on the dev machine leak into share links."""
    import app as app_module

    monkeypatch.setattr(app_module, "_ngrok_public_url", lambda: None)


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def new_client():
    app.config["TESTING"] = True
    return app.test_client()


def make_room(client, host="Alice", num_people=3, items=DEFAULT_ITEMS, extra=None):
    receipt = {
        "items": [{"name": n, "price": p} for n, p in items],
        "subtotal": "700",
        "total": "700",
        "extraction_meta": {"source": "vision_llm"},
    }
    data = {
        "host_name": host,
        "room_name": "Dinner",
        "num_people": str(num_people),
        "receipt_json": json.dumps(receipt),
    }
    data.update(extra or {})
    resp = client.post("/create", data=data)
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    match = re.search(r"/edit/([A-Z0-9]{6})", resp.headers["Location"])
    assert match, resp.headers["Location"]
    return match.group(1)


def join(client, code, name):
    return client.post(f"/join/{code}", data={"user_name": name})


def room(code):
    import storage

    with app.app_context():
        return storage.get_room(code)


def item_ids(code, *names):
    by_name = {i["name"]: i["id"] for i in room(code)["items"]}
    return [str(by_name[n]) for n in names]


def submit(client, code, name, item_names, submission_id=""):
    return client.post(
        f"/room/{code}/user/{name}/select-items",
        data={"selected_items": item_ids(code, *item_names), "submission_id": submission_id},
        headers={"X-Requested-With": "fetch"},
    )


def post_json(client, url, body=None):
    return client.post(url, json=body or {}, headers={"X-Requested-With": "fetch"})
