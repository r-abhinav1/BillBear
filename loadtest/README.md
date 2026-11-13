# Load test

`billbear.js` is a [k6](https://k6.io) script where each virtual user is one
table splitting a bill: the host opens a room from a saved scan (no photo, so no
paid Gemini call), guests join, every phone polls the room every 3 seconds while
picking (as the pages do), everyone sends picks, the host splits, and everyone
opens the results and downloads the PDF.

## The secret

The bot check and the per-network limits would stop a test from one machine
within seconds, so the server lets requests through that carry the
`LOAD_TEST_TOKEN` secret in an `X-Load-Test` header (`utils/security.py`). Scans
keep every limit. The secret is in `.env`; set the same value on the server only
for a test, and delete it straight after.

## Locally

```bash
# Keep the test out of Sentry: an empty DSN turns it off for this run.
SENTRY_DSN= .venv/bin/gunicorn -w 4 -k gthread --threads 8 -b 127.0.0.1:5055 app:app
k6 run -e BASE_URL=http://localhost:5055 -e LOAD_TEST_TOKEN=$(grep ^LOAD_TEST_TOKEN= .env | cut -d= -f2-) \
       -e TABLES=3 -e PHONES=4 -e PICK_SECONDS=9 -e RAMP=5s -e HOLD=20s loadtest/billbear.js
```

Keep local runs small: k6, the app, PDF rendering and Postgres all share one
machine. 20 tables of 6 phones was enough to stall a laptop.

## Against Vercel

Vercel's Hobby plan pauses the whole account after about 4 hours of Active CPU in
a month. Measured locally, a room poll costs about 8 ms of CPU and a PDF about
0.4 s; assume twice that on Vercel. 30 tables of 6 phones for 10 minutes is
roughly 13 CPU-minutes. Check the Usage page first.

```bash
k6 run -e BASE_URL=https://billbear.vercel.app -e CONFIRM_REMOTE=yes \
       -e LOAD_TEST_TOKEN=... -e TABLES=30 -e PHONES=6 -e PICK_SECONDS=60 -e RAMP=2m -e HOLD=6m \
       loadtest/billbear.js
```

Rooms are named `k6 load test …` and expire with the daily cleanup. Requests
from the test are tagged `load_test=true` in Sentry.

Knobs: `TABLES`, `PHONES`, `PICK_SECONDS`, `RAMP`, `HOLD`, `PDF` (0 skips PDFs).
