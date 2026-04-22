# BillBear — Smart Bill Splitting Made Simple

BillBear is a mobile-friendly web application that helps groups split restaurant bills fairly. The app extracts items using OCR and allows each person to select what they consumed. Tax, service charges, and discounts are distributed accurately to ensure a fair split for everyone.

Live Website: https://billbear.me  
Repository: https://github.com/r-abhinav1/BillBear

---

## Overview

- Scan receipts or manually enter items
- Select items per person with real-time collaboration
- Split the bill fairly with tax, service charges, and discounts included
- Export the final split as a downloadable PDF
- Works seamlessly on mobile devices
- Securely deployed on Vercel with persistent data storage using MongoDB Atlas

---

## Screenshot

---<img width="1920" height="1080" alt="Screenshot 2025-10-24 105641" src="https://github.com/user-attachments/assets/18540920-57b6-45eb-8316-e94577d4dccc" />


## Features

### Receipt OCR
- Upload receipt images to detect items automatically
- Manual editing supported for accuracy

### Room Creation & Collaboration
- Create a room with a unique code and share via link or QR
- Track who joined and submitted in real time
- Host can force-complete session if needed

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
| Database | MongoDB Atlas |
| OCR | Google Gemini Flash 2.0 |
| Deployment | Vercel |

---

## Installation (Local Development)

```bash
git clone https://github.com/r-abhinav1/BillBear.git
cd BillBear
pip install -r requirements.txt
cp .env.example .env
# edit .env once with your API keys
python app.py
# or
flask run
```

## OCR Extraction Architecture (Current)

BillBear now runs **OCR-text-first extraction**:

1. Browser OCR (`tesseract.js`) extracts raw text on the create-room page.
2. Backend deterministic parser normalizes receipt structure (`parse_receipt_text`).
3. By default, backend then runs LLM normalization on OCR text (`ALWAYS_USE_LLM_NORMALIZATION=true`):
   - Primary: Gemini
   - Automatic fallback: OpenRouter
4. Legacy image OCR path is available behind a feature flag for rollback (disabled by default).

Core modules:
- `utils/receipt_contracts.py`
- `utils/receipt_rules.py`
- `utils/receipt_parser.py`
- `utils/receipt_llm.py`
- `utils/receipt_pipeline.py`

## Environment Variables

Use a local `.env` file (copied from `.env.example`) so keys are loaded automatically on startup.

### Feature flags
- `OCR_TEXT_INGESTION_ENABLED` (default: `true`)
- `CLIENT_OCR_ENABLED` (default: `true`)
- `LEGACY_IMAGE_OCR_ENABLED` (default: `false`)
- `ALWAYS_USE_LLM_NORMALIZATION` (default: `true`; set `false` to allow confidence-based fallback)

### Parser / provider behavior
- `PARSER_CONFIDENCE_THRESHOLD` (default: `0.72`)
- `OCR_PROVIDER_TIMEOUT_SECONDS` (default: `20`)
- `OCR_PROVIDER_MAX_ATTEMPTS` (default: `2`)

### Gemini (primary)
- `GEMINI_API_KEYS` (comma-separated; preferred)
- `GEMINI_API_KEY` (single key; fallback)
- `GEMINI_TEXT_MODEL` (default: `gemini-2.0-flash`)
- `GEMINI_IMAGE_MODEL` (default: `gemini-2.0-flash`)

### OpenRouter (fallback)
- `OPENROUTER_API_KEY`
- `OPENROUTER_MODEL` (default: `openai/gpt-4o-mini`)
- `OPENROUTER_SITE_URL` (optional)
- `OPENROUTER_SITE_NAME` (optional, default: `BillBear`)
