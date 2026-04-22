# BillBear Codebase Analysis: Tech Stack, Gemini Limits, and Hosting/API Alternatives

## Executive Summary

BillBear is a **Python Flask monolith** with server-rendered HTML templates, using **Gemini 2.0 Flash** for receipt OCR extraction and **Redis** as the runtime data store for room state and API-key rotation. It is deployed to **Vercel Serverless Functions** (`@vercel/python`).

Your current pain points (Gemini limits + Vercel friction) are consistent with the current implementation:

- OCR is synchronous and latency-sensitive, with no timeout/retry/backoff on model calls.
- Key rotation is basic round-robin and not quota-aware.
- Vercel serverless constraints (function timeout, body-size limit, stateless execution, PDF dependency constraints) directly affect this workload.

If you want the **lowest migration risk**, use a **provider abstraction** and add **OpenRouter as one backend**, not as the only backend.  
If you want the **best long-term reliability**, move OCR execution off Vercel request path (worker/service), and host Flask on a container platform (Cloud Run / Render / Railway / Fly.io).

---

## What the Project Actually Uses (from code)

## Backend
- **Flask** app in `app.py`
- CORS enabled via `flask-cors`
- Routing and page rendering through Jinja templates (`templates/*.html`)

## OCR / AI
- `utils/tableMaker.py` calls:
  - `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent`
- Input image is base64-encoded and sent to Gemini with a strict JSON-output prompt.

## State & Data
- **Redis** is the active runtime datastore (`room:<code>` keys) for:
  - room data
  - users/selections/submission state
  - OCR API key list/index
- In-memory fallback exists when Redis is unavailable (not production-safe in serverless).

## PDF
- Preferred: `weasyprint` (local/non-Vercel path)
- Fallback: `xhtml2pdf` (used on Vercel path)

## Frontend
- Server-rendered HTML/CSS/JS
- Waiting room polls `/room/<code>/status` every 3s (client polling model)

## Deployment
- `vercel.json` routes all traffic to `app.py` via `@vercel/python`.

## Important mismatch
- `README.md` says MongoDB Atlas is used; current code uses **Redis**, not MongoDB.

---

## Code-Level Findings Driving Current Limits

1. **Gemini call robustness is minimal**
   - No request timeout on `requests.post(...)`.
   - No handling for 429/5xx with retry/backoff.
   - No structured fallback to alternate provider/model on failure.

2. **API key strategy helps, but does not solve project-level quotas**
   - Round-robin key selection in Redis.
   - Gemini limits are commonly enforced per project/tier dimensions (RPM/TPM/RPD), so rotating keys alone may not remove throttling.
   - Rotation update is not atomic quota-aware scheduling.

3. **Vercel constraints are workload-relevant**
   - Serverless timeout can be hit by OCR + image encode + downstream API latency.
   - 4.5 MB request/response body limit can reject large receipt uploads.
   - Stateless scale-out + polling can increase invocation volume and cost.
   - Native-heavy PDF stacks are constrained (already acknowledged in code).

4. **Configuration hygiene risk**
   - API key env name currently referenced as `gemini-api-key` (hyphenated).
   - Safer convention is uppercase underscore (e.g., `GEMINI_API_KEYS`) across platforms/tooling.

5. **Build/deploy portability risk**
   - `requirements.txt` is encoded as UTF-16 LE; this can cause packaging/install issues in some Linux CI/build environments.

---

## Viable API Alternatives (including OpenRouter)

| Option | Fit for BillBear | Pros | Cons | Recommendation |
|---|---|---|---|---|
| **OpenRouter (as gateway)** | High | Fastest path to multi-model fallback, single OpenAI-style integration, model routing flexibility | Adds gateway dependency, model behavior variance across providers | **Use as secondary/tertiary backend** behind abstraction |
| **Direct provider multi-home (OpenAI + Anthropic + Gemini)** | Very high | Highest control, predictable SLAs/pricing/telemetry, fewer intermediary risks | More integration work | **Best long-term architecture** |
| **Google Document AI / AWS Textract / Azure Document Intelligence** | High for receipts/forms | Strong extraction semantics for structured docs, often more stable than prompt-only parsing | Different output schema, migration mapping needed | **Best for OCR-heavy reliability goals** |
| **Self-host OCR (PaddleOCR/Tesseract) + LLM normalizer** | Medium | Lower vendor lock-in, potentially lower unit cost at scale | Ops burden + tuning complexity | Good only if infra ownership is acceptable |

### Is replacing with OpenRouter a good idea?

Yes, but as part of a **resilience layer**, not a hard swap.  
Best pattern:
- Provider abstraction (`extract_receipt(image) -> normalized schema`)
- Ordered fallback (Primary, Secondary, Tertiary)
- Per-provider timeout + retry budget + circuit breaker
- Unified JSON schema validation before app usage

---

## Viable Hosting Alternatives

| Hosting path | Suitability | Why |
|---|---|---|
| **Cloud Run (GCP)** | Excellent | Great for Python containers, longer requests, easy scale-to-zero, strong fit if staying near Gemini/Google stack |
| **Render / Railway / Fly.io** | Excellent | Simpler always-on app hosting for Flask + Redis; avoids serverless request-time constraints |
| **Keep Vercel, offload OCR worker** | Good transitional | Keep frontend/routes on Vercel, move heavy OCR and PDF to worker/service queue |
| **AWS Lambda + API Gateway** | Medium | Scalable but adds complexity; similar serverless constraints unless architecture is redesigned |

---

## Recommended Target Architecture (Pragmatic)

1. Keep Flask app behavior and templates.
2. Introduce an **OCR provider interface** and normalize output schema in one place.
3. Implement provider chain:
   - Primary: current Gemini (or upgraded model)
   - Secondary: OpenRouter model route
   - Optional third: OCR-specialized API (Doc AI / Textract / Azure DI)
4. Add resilience controls:
   - request timeouts
   - retry with exponential backoff on 429/5xx
   - circuit breaker on repeated provider failure
5. Move OCR execution off synchronous Vercel request path:
   - either migrate whole app to container host
   - or keep Vercel and add background worker endpoint/service

---

## Immediate, High-Impact Improvements (without full rewrite)

1. Rename env vars to portable conventions (`GEMINI_API_KEYS`, etc.).
2. Convert `requirements.txt` to UTF-8.
3. Add timeout/retry/status handling around model HTTP calls.
4. Add model/provider fallback chain before returning OCR failure.
5. Add upload validation/compression to stay below function body limits.
6. Correct README stack section (Redis vs MongoDB) to avoid operational confusion.

---

## Bottom Line

- **OpenRouter is viable** and useful, especially for quick multi-model fallback.
- **Better approach:** provider abstraction + multi-provider failover + explicit resilience controls.
- **Hosting:** for this OCR+PDF workflow, a container host (Cloud Run / Render / Railway / Fly.io) is generally a stronger fit than pure Vercel serverless-only execution.
