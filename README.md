# BillBear — Smart Bill Splitting Made Simple

BillBear is a mobile-friendly web application that helps groups split restaurant bills fairly. The app reads the bill's items with a vision AI model and allows each person to select what they consumed. Tax, service charges, and discounts are distributed accurately to ensure a fair split for everyone.

Live Website: https://billbear.vercel.app  
Repository: https://github.com/r-abhinav1/BillBear

---

## Overview

- Scan receipts or manually enter items
- Select items per person with real-time collaboration
- Split the bill fairly with tax, service charges, and discounts included
- Export the final split as a downloadable PDF
- Works seamlessly on mobile devices
- Deployed on Vercel with Postgres (Supabase, Mumbai) storage

---

## Screenshot

---<img width="1920" height="1080" alt="Screenshot 2025-10-24 105641" src="https://github.com/user-attachments/assets/18540920-57b6-45eb-8316-e94577d4dccc" />


## Features

### Receipt Scanning
- Upload receipt images to detect items automatically
- Manual editing supported for accuracy

### Room Creation & Collaboration
- Create a room with a unique code and share via link or QR
- Track who joined and submitted in real time
- Host finalizes once everyone has submitted (or lowers the party size for no-shows)

### Fair Cost Distribution
- Items shared only among selected users
- Tax, service charges, and discounts proportionally distributed
- Per-user breakdown shown in results

### PDF Export
- Clean PDF summary of bill split downloadable by users

### SEO & PWA Support
- Dynamic sitemaps and metadata for discoverability
- PWA-enabled for app-like mobile experience

---

## Tech Stack

| Component | Technology |
|----------|------------|
| Backend | Flask |
| Frontend | HTML, CSS, JavaScript |
| Database | Postgres (Supabase) |
| Receipt scanning | Gemini vision (OpenRouter fallback) |
| Deployment | Vercel |

---

## Project layout

```
app.py                  Entry point: app = create_app(). Vercel imports it (vercel.json).
run.py                  Local launcher (checks the venv and the port, then runs app.py).
billbear/               The application package
  __init__.py           create_app(): config, database, template filters, hooks, blueprints
  config.py             Paths and settings fixed at start-up
  db.py                 One Postgres connection per request
  storage.py            Every query; one transaction per write, room row locked
  routes/               Blueprints, one per area
    pages.py            Home, help, terms, health check
    scan.py             POST /api/receipt/scan
    rooms.py            Create a room, edit its bill, room page, live status
    join.py             Joining, the host recovery link
    picks.py            Picking dishes, waiting room
    host.py             The host's controls and Split
    results.py          Results page and PDF download
    cron.py             Daily clean-up
  bill/                 Bill maths: no Flask, no database
    money.py totals.py split.py audit.py portions.py validation.py assembler.py
  scan/                 Reading a receipt photo
    image_prep.py pipeline.py llm.py rules.py contracts.py
  pdf/                  render.py (the split as a PDF), text.py (non-Latin fonts)
  web/                  Request plumbing
    access.py           Loading a room, who is calling, rate limits
    errors.py hooks.py  Error pages; CSRF check, headers, metrics around each request
    security.py session.py observability.py sharing.py
templates/  static/     Jinja templates and static files
migrations/             SQL migrations, applied in order by scripts/migrate.py
tests/                  pytest; integration tests need TEST_DATABASE_URL
loadtest/               k6 load test
```

## Local Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env          # then set SECRET_KEY and (optionally) GEMINI_API_KEYS

# Postgres: any local instance works; e.g. Docker
docker run -d --name billbear-pg -e POSTGRES_USER=billbear -e POSTGRES_PASSWORD=billbear \
  -e POSTGRES_DB=billbear -p 54329:5432 postgres:16-alpine
docker exec billbear-pg psql -U billbear -c "create database billbear_test;"

.venv/bin/python scripts/migrate.py                       # dev DB (DATABASE_URL_UNPOOLED / DATABASE_URL)
.venv/bin/python scripts/migrate.py "$TEST_DATABASE_URL"  # test DB
.venv/bin/python -m pytest                                # integration tests need TEST_DATABASE_URL
python3 run.py                                            # http://localhost:5001
```

Without `GEMINI_API_KEYS` the scan endpoint returns "not configured" and hosts use **Enter items manually**.

Scanning uses **Gemini 3.1 Flash-Lite** (`GEMINI_IMAGE_MODEL`), about ₹0.15 for a typical bill with GST, rising by about ₹0.004 a dish (₹0.85 for a 200-dish bill; output tokens are most of it). `SCAN_DAILY_CAP` (default 15; production uses 200) caps total scans per day across all users. Each network (IP) gets at most 5 scans per 10 minutes and 10 per day (`RATE_LIMITS` in `billbear/web/security.py`). Past a limit, users get "enter items manually". Each scan logs its token usage (`[receipt_llm] gemini usage ...`), so you can check real costs in the Vercel logs.

## How it works

- **Scan:** the browser downscales the photo (never upscales) and posts it to `POST /api/receipt/scan`, which validates and re-encodes it (Pillow) and asks Gemini vision for the items within a ~30s budget (one random key, a second key only on 429/5xx/timeout, then OpenRouter best effort). The host always reviews items on the edit screen.
- **Storage:** normalized tables (`rooms`, `participants`, `items`, `claims`, `rate_limits`) in `migrations/001_init.sql`. Every write runs in a transaction that locks the room row, so simultaneous submits/joins/finalize resolve to one consistent outcome. Rooms expire 24h after the last write; a daily Vercel cron (`/api/cron/cleanup`, Bearer `CRON_SECRET`) writes a heartbeat row (`app_heartbeat`) and deletes expired rooms.
- **Flow:** join → pick items → host taps **Finalize** (only when everyone has joined and submitted) → results/PDF. Results are hidden until finalized; the host can reopen.

## Security model (no accounts)

- **The room link is the invitation.** Anyone with the code can join (rate-limited; repeated wrong codes from one IP get 429).
- **Your device is your identity in a room.** Joining mints a random id; the DB stores only its SHA-256 and the browser gets it in a signed, HttpOnly, SameSite=Lax cookie. Every write checks cookie → DB row; names in URLs are never trusted.
- **The host** has the same cookie plus a private **host link** (shown on the host's room page) that restores host rights on another device. If a guest loses their phone, the host taps **Move them to a new phone** and passes on the one-time move code (or its link); the guest rejoins with the same name and the code.
- **Abuse controls:** per-IP rate limits in Postgres, a short-lived scan ticket cookie for the paid scan endpoint, optional Cloudflare Turnstile (`TURNSTILE_*`), same-origin checks on all POSTs, CSP / X-Frame-Options / Referrer-Policy headers, strict name validation, and the app refuses to start on Vercel without a strong `SECRET_KEY`.

## Deploying (Vercel + Supabase)

1. Create a Supabase project in **Mumbai (ap-south-1)**, and pin the Vercel functions to Mumbai too (`"regions": ["bom1"]` in `vercel.json`) — the latency that matters is function ↔ database. Optionally a second project for previews.
2. Set env vars per environment in Vercel: `DATABASE_URL` = **transaction pooler** URL (port 6543), `DATABASE_URL_UNPOOLED` = **session pooler** URL (port 5432), plus `SECRET_KEY`, `CRON_SECRET`, `GEMINI_API_KEYS`. Preview must not point at the production project.
3. Run `scripts/migrate.py` against each project (session pooler URL).
4. Deploy; check the cron appears under the project's Cron Jobs settings. **The daily cron is what stops Supabase pausing the free project after ~7 days idle** — a paused project must be restored manually from the dashboard.
5. Verify the keep-alive end to end (crons only run on the **Production** deployment):
   - Trigger it once by hand: `curl -H "Authorization: Bearer $CRON_SECRET" https://<your-domain>/api/cron/cleanup` → `{"status":"success","heartbeat_count":...}`.
   - The next day, in the Supabase SQL editor: `SELECT * FROM app_heartbeat;` — `last_run` should be within the last 24h. Vercel also shows each run under Settings → Cron Jobs → View logs.
5. Only Postgres is used: no Supabase Auth, REST API or anon key.

See `.env.example` for every setting.
