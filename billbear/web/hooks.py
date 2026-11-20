"""What runs around every request: the CSRF origin check, security headers, metrics."""
from flask import g, request

from billbear.web.errors import error_response
from billbear.web.observability import flush_if_marked, tag
from billbear.web.security import apply_security_headers, is_same_origin, load_test_pass, turnstile_enabled
from billbear.web.session import set_human_pass


UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _flush_rare_metrics(_exc):
    flush_if_marked()


def _mark_load_test():
    # The k6 load test (loadtest/) identifies itself with a secret header; see load_test_pass.
    g.load_test = load_test_pass(request)
    if g.load_test:
        tag(load_test=True)


def _csrf_origin_check():
    if request.method in UNSAFE_METHODS and not is_same_origin(request):
        return error_response("Cross-site request blocked.", 403, "BAD_ORIGIN")
    return None


def _security_headers(response):
    return apply_security_headers(response, turnstile=turnstile_enabled())


def _remember_human(response):
    # A scan whose bot-check token Cloudflare accepted: let Create skip a second
    # token for a while (see web/session.py). Set on every response of that
    # request, so it isn't lost if the scan itself then fails.
    if g.get("human_verified"):
        set_human_pass(response)
    return response


def register_request_hooks(app):
    app.teardown_request(_flush_rare_metrics)
    app.before_request(_mark_load_test)
    app.before_request(_csrf_origin_check)
    # after_request functions run in reverse order: _remember_human, then the headers.
    app.after_request(_security_headers)
    app.after_request(_remember_human)
