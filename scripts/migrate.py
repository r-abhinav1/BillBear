#!/usr/bin/env python3
"""
Apply SQL migrations in migrations/ (lexical order), once each.

Usage:
    .venv/bin/python scripts/migrate.py            # uses DATABASE_URL_UNPOOLED, else DATABASE_URL
    .venv/bin/python scripts/migrate.py <url>      # explicit URL

Use Supabase's *session pooler* URL here (port 5432 on the pooler host): it
behaves like a direct connection for DDL and, unlike the free-tier direct
connection, works over IPv4.
"""
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "migrations"


def apply_migrations(url: str, verbose: bool = True) -> list:
    applied = []
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version text PRIMARY KEY,"
            " applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = path.stem
            if version in done:
                continue
            with conn.transaction():
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
            applied.append(version)
            if verbose:
                print(f"applied {version}")
    if verbose and not applied:
        print("database is up to date")
    return applied


def main() -> None:
    load_dotenv(ROOT / ".env")
    url = (
        sys.argv[1] if len(sys.argv) > 1
        else os.getenv("DATABASE_URL_UNPOOLED") or os.getenv("DATABASE_URL")
    )
    if not url:
        sys.exit("Set DATABASE_URL_UNPOOLED or DATABASE_URL (or pass a URL).")
    apply_migrations(url)


if __name__ == "__main__":
    main()
