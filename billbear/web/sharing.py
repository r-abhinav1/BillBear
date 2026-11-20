"""Links other people open: the join link and its QR code, and the host's recovery link."""
import base64
import os
from io import BytesIO

import qrcode
from flask import request, url_for

from billbear.config import on_vercel
from billbear.web.session import read_hostkey_cookie


def _lan_ip():
    """This machine's Wi-Fi/LAN address (no packets are sent)."""
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
    except OSError:
        return None


_NGROK_CACHE = {"url": None, "checked": 0.0}


def _ngrok_public_url():
    """
    Local dev only: the https URL of a running `ngrok http` tunnel, read from
    ngrok's local agent API (localhost:4040). Cached for 30s; None if no tunnel.
    """
    import time

    import requests as _requests

    now = time.monotonic()
    if now - _NGROK_CACHE["checked"] < 30:
        return _NGROK_CACHE["url"]
    url = None
    try:
        tunnels = _requests.get("http://127.0.0.1:4040/api/tunnels", timeout=0.3).json().get("tunnels", [])
        https = [t.get("public_url", "") for t in tunnels if str(t.get("public_url", "")).startswith("https://")]
        url = https[0].rstrip("/") if https else None
    except Exception:
        url = None
    _NGROK_CACHE.update(url=url, checked=now)
    return url


def share_url(endpoint: str, **values) -> str:
    """
    Absolute URL for links other people open (QR code, join link).
    Order: PUBLIC_BASE_URL if set. In local dev, when the page was opened via
    localhost: a running ngrok tunnel (works on mobile data too), else this
    machine's Wi-Fi address (same network only). Otherwise the current host.
    """
    path = url_for(endpoint, **values)
    base = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if base:
        return base + path
    host = request.host.split(":")[0]
    if not on_vercel() and host in {"localhost", "127.0.0.1", "::1", "[::1]"}:
        tunnel = _ngrok_public_url()
        if tunnel:
            return tunnel + path
        lan = _lan_ip()
        if lan:
            port = request.host.split(":")[1] if ":" in request.host else ""
            return f"{request.scheme}://{lan}{':' + port if port else ''}{path}"
    return url_for(endpoint, _external=True, **values)


def generate_qr_base64(link: str) -> str:
    buf = BytesIO()
    qrcode.make(link).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def host_recovery_url(room_code: str):
    token = read_hostkey_cookie(request, room_code)
    if not token:
        return None
    return url_for("join.host_recover", room_code=room_code, k=token, _external=True)
