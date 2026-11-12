# BillBear — Smart Bill Splitting Made Simple

BillBear is a mobile-friendly web application that helps groups split restaurant bills fairly. The app reads the bill's items with a vision AI model and allows each person to select what they consumed. Tax, service charges, and discounts are distributed accurately to ensure a fair split for everyone.

Live Website: https://billbear.me  
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

## Local Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
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

Scanning uses **Gemini 3.1 Flash-Lite** (`GEMINI_IMAGE_MODEL`), about ₹0.15 per bill. `SCAN_DAILY_CAP` (default 15) caps total scans per day across all users, about ₹800 a year before GST. Past the cap, users get "enter items manually". Each scan logs its token usage (`[receipt_llm] gemini usage ...`), so you can check real costs in the Vercel logs.

## How it works

- **Scan:** the browser downscales the photo (never upscales) and posts it to `POST /api/receipt/scan`, which validates and re-encodes it (Pillow) and asks Gemini vision for the items within a ~25s budget (one random key, a second key only on 429/5xx/timeout, then OpenRouter best effort). The host always reviews items on the edit screen.
- **Storage:** normalized tables (`rooms`, `participants`, `items`, `claims`, `rate_limits`) in `migrations/001_init.sql`. Every write runs in a transaction that locks the room row, so simultaneous submits/joins/finalize resolve to one consistent outcome. Rooms expire 24h after the last write; a daily Vercel cron (`/api/cron/cleanup`, Bearer `CRON_SECRET`) writes a heartbeat row (`app_heartbeat`) and deletes expired rooms.
- **Flow:** join → pick items → host taps **Finalize** (only when everyone has joined and submitted) → results/PDF. Results are hidden until finalized; the host can reopen.

## Security model (no accounts)

- **The room link is the invitation.** Anyone with the code can join (rate-limited; repeated wrong codes from one IP get 429).
- **Your device is your identity in a room.** Joining mints a random id; the DB stores only its SHA-256 and the browser gets it in a signed, HttpOnly, SameSite=Lax cookie. Every write checks cookie → DB row; names in URLs are never trusted.
- **The host** has the same cookie plus a private **host link** (shown on the host's room page) that restores host rights on another device. If a guest loses their phone, the host taps **Reset device** and the guest rejoins with the same name.
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
