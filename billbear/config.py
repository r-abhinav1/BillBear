"""
Settings fixed when the app starts.

Limits and switches that tests or operators change at run time (rate limits,
the scan cap, cookie security, Turnstile, the room TTL) are still read from the
environment where they are used, on each request.
"""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = PROJECT_ROOT / "templates"
STATIC_DIR = PROJECT_ROOT / "static"

# Receipt photos are capped at 4 MB (scan/image_prep.py); a request a bit above that.
MAX_CONTENT_LENGTH = 5 * 1024 * 1024


def on_vercel() -> bool:
    return bool(os.getenv("VERCEL"))


def trust_proxy() -> bool:
    """Behind Vercel or ngrok, TLS ends at the proxy: trust its X-Forwarded-* headers."""
    return on_vercel() or os.getenv("TRUST_PROXY", "").lower() in {"1", "true", "yes"}
