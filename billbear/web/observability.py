"""
Error tracking, tracing, logs and metrics with Sentry, off unless SENTRY_DSN is set.

What leaves the app is kept to what a bug report needs:
- no request bodies (names, receipts), cookies, headers with secrets, or IPs,
  and no variables in stack traces;
- URLs by route, never with the values in them: a guest's
  /room/AB12CD/user/Asha/select-items is sent as
  /room/<room_code>/user/<user_name>/select-items, and query strings are dropped.

Expected outcomes (a full room, a bad price, a 404 for a wrong code) are not
errors and are never sent; crashes, database outages, PDF failures and scan
billing problems are.

Vercel may freeze an instance as soon as a response is sent, so anything worth
keeping is flushed before replying (`capture` does this); traces of ordinary
requests are best effort.
"""
import os
from typing import Any, Dict, Optional

_enabled = False


def _sample_rate(vercel: bool) -> float:
    """Share of requests traced: all of them locally, 10% on Vercel (SENTRY_TRACES_SAMPLE_RATE overrides)."""
    default = 0.1 if vercel else 1.0
    try:
        return min(1.0, max(0.0, float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", default))))
    except ValueError:
        return default


def _scrub(event: Dict[str, Any], hint: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Strip anything personal from an error or transaction before it is sent."""
    req = event.get("request")
    if isinstance(req, dict):
        req["url"] = _route_url(event, req.get("url"))
        if not req["url"]:
            req.pop("url")
        for key in ("query_string", "data", "cookies", "env"):
            req.pop(key, None)
        headers = req.get("headers")
        if isinstance(headers, dict):
            req["headers"] = {k: v for k, v in headers.items()
                              if k.lower() in {"user-agent", "accept", "content-type", "x-requested-with"}}
    event.pop("user", None)
    return event


def _route_url(event: Dict[str, Any], url: Optional[str]) -> Optional[str]:
    """
    The URL with its values replaced by the route's placeholders. During a
    request Flask knows the route; a transaction is sent after the request has
    ended, but its name is the route (transaction_style="url"). Anything else
    loses its URL rather than risk a name in it.
    """
    from urllib.parse import urlsplit

    base = ""
    if url:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}" if parts.scheme else ""
    try:
        from flask import has_request_context, request

        if has_request_context() and request.url_rule is not None:
            return request.host_url.rstrip("/") + request.url_rule.rule
    except Exception:
        pass
    name = event.get("transaction")
    if isinstance(name, str) and name.startswith("/"):
        return base + name
    return None


def init_sentry(vercel: bool) -> bool:
    """Start Sentry if SENTRY_DSN is set. Returns whether it is on."""
    global _enabled
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return False
    import logging

    import sentry_sdk
    from sentry_sdk.integrations.flask import FlaskIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration

    sentry_sdk.init(
        dsn=dsn,
        environment=os.getenv("SENTRY_ENVIRONMENT") or os.getenv("VERCEL_ENV") or ("production" if vercel else "development"),
        release=os.getenv("VERCEL_GIT_COMMIT_SHA") or None,
        # Log lines become breadcrumbs, not events: `capture` reports each failure
        # once, tagged with what kind it was, instead of a second "Unhandled error".
        # Warnings and errors are also sent to Sentry Logs (searchable, not issues).
        integrations=[FlaskIntegration(transaction_style="url"),
                      LoggingIntegration(level=logging.INFO, event_level=None, sentry_logs_level=logging.WARNING)],
        enable_logs=True,
        traces_sample_rate=_sample_rate(vercel),
        send_default_pii=False,
        max_request_body_size="never",
        # Stack traces would otherwise carry each frame's variables: the request,
        # names from the URL, form fields. The code lines around a crash stay.
        include_local_variables=False,
        before_send=_scrub,
        before_send_transaction=_scrub,
    )
    _set_high_volume_rate(vercel)
    _enabled = True
    return True


def enabled() -> bool:
    return _enabled


def capture(exc: BaseException, **tags: Any) -> None:
    """Send an exception now (with tags), and wait briefly so a frozen instance can't lose it."""
    if not _enabled:
        return
    import sentry_sdk

    with sentry_sdk.new_scope() as scope:
        for key, value in tags.items():
            scope.set_tag(key, value)
        sentry_sdk.capture_exception(exc)
    sentry_sdk.flush(timeout=2)


def tag(**tags: Any) -> None:
    """Tag everything sent for the current request (e.g. load_test=True)."""
    if not _enabled:
        return
    import sentry_sdk

    for key, value in tags.items():
        sentry_sdk.get_current_scope().set_tag(key, value)


def flush() -> None:
    if _enabled:
        import sentry_sdk

        sentry_sdk.flush(timeout=2)


# The daily cleanup cron, as a Sentry cron monitor: Sentry emails when a day is missed.
CRON_MONITOR_SLUG = "daily-cleanup"
CRON_MONITOR_CONFIG = {
    "schedule": {"type": "crontab", "value": "17 3 * * *"},   # vercel.json
    "timezone": "UTC",
    "checkin_margin": 90,      # Vercel Hobby runs the job somewhere in that hour
    "max_runtime": 5,
    "failure_issue_threshold": 1,
    "recovery_threshold": 1,
}


def cron_monitor():
    """Context manager for the cron body; does nothing when Sentry is off."""
    if not _enabled:
        from contextlib import nullcontext

        return nullcontext()
    from sentry_sdk.crons import monitor

    return monitor(monitor_slug=CRON_MONITOR_SLUG, monitor_config=CRON_MONITOR_CONFIG)


# ---------------------------------------------------------------------------
# Metrics (Sentry > Explore > Metrics). Names are "area.thing"; attributes are
# low-cardinality labels to group by, never names, room codes or amounts.
# ---------------------------------------------------------------------------

_HIGH_VOLUME_RATE = 1.0


def _set_high_volume_rate(vercel: bool) -> None:
    """Share of per-request metrics (polls, DB connects) actually sent: 10% on Vercel, all locally."""
    global _HIGH_VOLUME_RATE
    default = 0.1 if vercel else 1.0
    try:
        _HIGH_VOLUME_RATE = min(1.0, max(0.0, float(os.getenv("SENTRY_METRICS_SAMPLE_RATE", default))))
    except ValueError:
        _HIGH_VOLUME_RATE = default


# Rare events worth not losing: the SDK sends metrics in batches on a timer, and
# Vercel can freeze an instance before a batch goes out (the load test lost ~2%
# of splits and PDFs). Requests that record one of these flush before they end.
# Polls and DB connects happen constantly, so they never wait for a flush.
FLUSH_AFTER = {
    "scan.result", "room.created", "bill.split", "bill.fix", "picks.reopened",
    "pdf.download", "pdf.render", "cron.rooms_deleted",
}


def _attrs(name: str, attrs: Dict[str, Any]) -> Dict[str, Any]:
    clean = {k: v for k, v in attrs.items() if v is not None}
    try:
        from flask import g, has_request_context

        if has_request_context():
            # Text, not a boolean: Sentry's metric search can filter on it then.
            clean["load_test"] = "true" if g.get("load_test") else "false"
            if name in FLUSH_AFTER:
                g.sentry_flush = True
    except Exception:
        pass
    return clean


def count(name: str, value: float = 1, **attrs: Any) -> None:
    """How many times something happened: scans, rooms, splits."""
    if _enabled:
        import sentry_sdk

        sentry_sdk.metrics.count(name, value, attributes=_attrs(name, attrs))


def distribution(name: str, value: float, unit: Optional[str] = None, **attrs: Any) -> None:
    """One measured value among many, where percentiles matter: durations, sizes."""
    if _enabled:
        import sentry_sdk

        sentry_sdk.metrics.distribution(name, value, unit=unit, attributes=_attrs(name, attrs))


def flush_if_marked() -> None:
    """End of request: send now if it recorded something in FLUSH_AFTER."""
    if not _enabled:
        return
    try:
        from flask import g

        if g.get("sentry_flush"):
            import sentry_sdk

            sentry_sdk.flush(timeout=1)
    except Exception:
        pass


def count_sampled(name: str, **attrs: Any) -> None:
    """A count for something that happens on every request; sampled, and scaled so totals stay right."""
    import random

    rate = _HIGH_VOLUME_RATE
    if _enabled and rate > 0 and random.random() < rate:
        count(name, 1 / rate, **attrs)


def distribution_sampled(name: str, value: float, unit: Optional[str] = None, **attrs: Any) -> None:
    import random

    rate = _HIGH_VOLUME_RATE
    if _enabled and rate > 0 and random.random() < rate:
        distribution(name, value, unit, **attrs)
