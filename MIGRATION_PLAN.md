# BillBear Migration Plan: Client OCR + Backend Parser + LLM Fallback

## Problem statement

The current extraction path is image-first via Gemini Vision (`utils/tableMaker.py`), which couples OCR and structuring to a single synchronous model call. This increases rate-limit exposure, latency sensitivity, and Vercel function pressure.  
Goal: migrate to a no-image-storage architecture where OCR happens client-side, backend receives text payloads, deterministic parsing handles most receipts, and LLM is used only for ambiguous cases.

## Proposed approach

Move from **LLM-first image extraction** to **OCR-first text extraction** while preserving the existing room flow and bill-splitting logic.  
Key principles:
- Do not persist uploaded bill images.
- Keep deterministic split math authoritative.
- Use provider abstraction and LLM fallback only when parser confidence is low.
- Use **Gemini as primary and OpenRouter as automatic fallback** when Gemini is unavailable or throttled.
- Preserve manual correction UX (`edit_items.html`) as a safety net.

## Current prompt and rules location (verified)

- **LLM prompt location:** `utils/tableMaker.py` inside `ocrBillMaker.__init__` as `self.prompt`.
- **Formatting rules location:** embedded in the same `self.prompt` block under `"Expected JSON format"` and `"Rules:"`.
- **No separate rules file exists** in the repository today.

## Phase tracker

| Phase | Name | Status | Completion criteria |
|---|---|---|---|
| 1 | Contracts and rules baseline | Completed | Input/output schemas and rule source finalized and documented |
| 2 | Backend parser pipeline | Completed | Text parser + validation + confidence path working end-to-end |
| 3 | Multi-provider LLM fallback | In progress | Gemini primary and OpenRouter fallback policy implemented |
| 4 | Frontend OCR ingestion | In progress | Browser OCR sends text payload; no image persistence in backend flow |
| 5 | Route integration and compatibility | In progress | Existing room/join/results flow works with OCR-text path |
| 6 | Rollout, observability, docs | In progress | Metrics, feature flag rollout, and docs/config updates completed |

## Detailed phases

### Phase 1 — Contracts and rules baseline (**Status: Completed**)
- Define canonical OCR input payload schema (`raw_text`, optional `lines`, confidence, metadata).
- Define canonical normalized receipt schema (restaurant/date/time/items/subtotal/taxes/discount/total).
- Decide strict validation rules and malformed-payload error behavior.
- Extract and centralize rules currently embedded in `utils/tableMaker.py` `self.prompt` into a reusable rules/prompt module.

### Phase 2 — Backend parser pipeline (**Status: Completed**)
- Refactor extraction into `parse_receipt_text` -> `validate_receipt` -> `llm_fallback_if_needed`.
- Keep output shape compatible with current `room['ocr_data']` usage.
- Implement deterministic parsing for Indian receipt patterns: item lines, subtotal/total, service charge, discount, CGST/SGST/IGST.
- Handle OCR noise (merged lines, currency/decimal artifacts) and generate parser confidence + unresolved fields.

### Phase 3 — Multi-provider LLM fallback (**Status: In progress**)
- Add provider adapter for text-to-JSON normalization.
- Configure **Gemini as primary** and **OpenRouter as automatic fallback**.
- Trigger fallback on Gemini timeout, 429 rate-limit, and 5xx availability failures.
- Enforce timeout/retry/backoff and JSON schema validation on all provider outputs.

### Phase 4 — Frontend OCR ingestion (**Status: In progress**)
- Add client OCR flow on create path (capture/upload -> preprocess -> OCR text extraction).
- Send text payload to backend endpoint instead of image file in primary mode.
- Keep legacy image-upload path behind a feature flag for rollback.

### Phase 5 — Route integration and compatibility (**Status: In progress**)
- Add/modify `app.py` endpoints for OCR-text ingestion and room creation.
- Ensure `items` and `ocr_data` remain compatible with templates/results.
- Preserve existing room, join, waiting, and results behavior.

### Phase 6 — Rollout, observability, docs (**Status: In progress**)
- Add logging/metrics: parser-only success, fallback usage, extraction failures, and source mode.
- Roll out with feature flag and phased enablement.
- Normalize env vars (`GEMINI_API_KEYS`, provider toggles, timeouts).
- Update README/runtime notes and provider outage playbook.

## Key files/components expected to change

- `app.py` (route and pipeline orchestration)
- `utils/tableMaker.py` (decompose/refactor provider logic)
- `templates/create_room.html` and/or related client JS (frontend OCR submission flow)
- `templates/edit_items.html` (small compatibility updates only if schema evolves)
- `README.md` (architecture and deployment notes)

## Notes and decisions

- Existing `calculate_bill_split()` remains the source of truth for split math.
- Image storage is avoided by design; process images in-browser and discard.
- LLM token size is not the core constraint for receipt text; reliability focus should be on retries, fallback, and parser coverage.
- Multi-provider resiliency is required: Gemini primary, OpenRouter fallback.
- Keep legacy path during migration to reduce rollout risk.

## Implementation progress (current iteration)

- Added canonical receipt contracts and shared prompt/rule module under `utils/receipt_contracts.py` and `utils/receipt_rules.py`.
- Implemented deterministic OCR-text parser with confidence scoring and unresolved-field tracking in `utils/receipt_parser.py`.
- Added text normalization provider adapters with Gemini primary + OpenRouter fallback in `utils/receipt_llm.py`.
- Added extraction orchestration (`parse_receipt_text -> validate -> llm_fallback_if_needed`) in `utils/receipt_pipeline.py`.
- Updated `app.py` with OCR-text ingestion endpoint (`/api/ocr/ingest`) and `/create` OCR-text-first flow.
- Added browser OCR ingestion in `templates/create_room.html` using Tesseract.js; payload submitted as `ocr_payload`.
- Extended schema compatibility for IGST across edit/results templates and bill split calculation.
