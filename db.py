"""
Postgres connection handling.

One connection per request, stored on flask.g and closed in teardown. On
Vercel use Supabase's *transaction pooler* URL (Supavisor, port 6543), which is
why prepared statements are disabled (prepare_threshold=None).

Connections are autocommit; multi-statement writes use `conn.transaction()`.
Never keep a module-level connection: Fluid Compute reuses instances across
concurrent requests.
"""
import os
import time

import psycopg
from flask import g
from psycopg.rows import dict_row


class DatabaseUnavailable(Exception):
    """Raised when we cannot reach Postgres (mapped to HTTP 503)."""


def database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise DatabaseUnavailable("DATABASE_URL is not configured")
    return url


def _connect() -> psycopg.Connection:
    url = database_url()
    last_exc = None
    # Transient pooler/network hiccup: retry once.
    for attempt in range(2):
        try:
            return psycopg.connect(
                url,
                autocommit=True,
                row_factory=dict_row,
                prepare_threshold=None,
                connect_timeout=5,
            )
        except psycopg.OperationalError as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(0.4)
    print(f"[db] connect failed: {last_exc}")
    raise DatabaseUnavailable("Database is unavailable") from last_exc


def get_conn() -> psycopg.Connection:
    conn = g.get("_db_conn")
    if conn is None or conn.closed:
        conn = _connect()
        g._db_conn = conn
    return conn


def close_conn(exc=None) -> None:
    conn = g.pop("_db_conn", None)
    if conn is None:
        return
    try:
        if not conn.closed:
            conn.close()
    except Exception as close_exc:  # pragma: no cover - best effort
        print(f"[db] close failed: {close_exc}")


def init_app(app) -> None:
    app.teardown_appcontext(close_conn)
