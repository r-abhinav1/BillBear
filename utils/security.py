"""
Request-level security helpers: client IP, same-origin checks, security
headers, rate limits and the optional bot check.
"""
import os
from typing import Optional
from urllib.parse import urlsplit

import requests

def scan_daily_cap() -> int:
    """Total scans per day across ALL users: a hard ceiling on vision spend."""
    try:
        return max(0, int(os.getenv("SCAN_DAILY_CAP", "15")))
    except ValueError:
        return 15


# name -> (limit, window_seconds); counted per client IP
RATE_LIMITS = {
    "scan": (12, 600),        # vision calls cost money
    "scan_day": (60, 86400),
    "create": (20, 3600),
    "join": (40, 600),
    "miss": (30, 600),        # 404s on room codes / bad host links (code guessing)
}


def client_ip(request) -> str:
    """On Vercel the platform sets x-forwarded-for / x-real-ip; locally use the socket."""
    if os.getenv("VERCEL"):
        real = request.headers.get("x-real-ip", "").strip()
        if real:
            return real
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _origin_of(url: str) -> Optional[str]:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def is_same_origin(request) -> bool:
    """
    CSRF belt-and-braces on top of SameSite=Lax cookies. If the browser sent
    Origin (or Referer), it must match this host. Requests with neither are
    allowed (some privacy tools strip both); Lax cookies still protect them.
    """
    expected = _origin_of(request.host_url)
    origin = request.headers.get("Origin")
    if origin:
        return origin.lower() != "null" and _origin_of(origin) == expected
    referer = request.headers.get("Referer")
    if referer:
        return _origin_of(referer) == expected
    return True


def apply_security_headers(response, turnstile: bool = False):
    script_src = "'self' 'unsafe-inline'"
    frame_src = "'none'"
    connect_src = "'self'"
    if turnstile:
        script_src += " https://challenges.cloudflare.com"
        frame_src = "https://challenges.cloudflare.com"
    csp = (
        "default-src 'self'; "
        f"script-src {script_src}; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: blob:; "
        f"connect-src {connect_src}; "
        f"frame-src {frame_src}; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
    )
    headers = response.headers
    headers.setdefault("Content-Security-Policy", csp)
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("Referrer-Policy", "same-origin")
    headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
    if os.getenv("VERCEL"):
        headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


# ---------------------------------------------------------------------------
# Bot check (Cloudflare Turnstile). Disabled unless TURNSTILE_SECRET_KEY is set.
# ---------------------------------------------------------------------------

def turnstile_site_key() -> str:
    return os.getenv("TURNSTILE_SITE_KEY", "").strip()


def turnstile_enabled() -> bool:
    return bool(os.getenv("TURNSTILE_SECRET_KEY", "").strip() and turnstile_site_key())


def verify_turnstile(token: str, remote_ip: str) -> bool:
    if not turnstile_enabled():
        return True
    if not token:
        return False
    try:
        resp = requests.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={
                "secret": os.getenv("TURNSTILE_SECRET_KEY", "").strip(),
                "response": token,
                "remoteip": remote_ip,
            },
            timeout=4,
        )
        return bool(resp.ok and resp.json().get("success"))
    except Exception as exc:
        print(f"[security] turnstile verify failed: {exc}")
        return False
