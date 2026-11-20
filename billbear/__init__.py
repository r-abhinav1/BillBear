"""
BillBear: split a restaurant bill by what each person ate.

`create_app()` builds the Flask app. The project root's app.py calls it, and
Vercel imports `app` from there (vercel.json).
"""
import sys

from dotenv import load_dotenv
from flask import Flask

load_dotenv()

# Write log lines immediately even when stdout is redirected to a file.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass


class DecodedPathInfo:
    """WSGI middleware: percent-decode PATH_INFO once (see where it is applied)."""

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        from urllib.parse import unquote_to_bytes

        path = environ.get("PATH_INFO", "")
        if "%" in path:
            # WSGI strings carry raw bytes as latin-1; Werkzeug decodes them as UTF-8.
            environ["PATH_INFO"] = unquote_to_bytes(path).decode("latin-1")
        return self.wsgi_app(environ, start_response)


def create_app() -> Flask:
    from billbear import config, db
    from billbear.bill.money import currency_choices, format_amount, format_money, get_currency
    from billbear.bill.totals import MAX_CHARGES
    from billbear.bill.validation import MAX_PEOPLE, MIN_PEOPLE
    from billbear.pdf.text import pdf_text
    from billbear.routes import BLUEPRINTS
    from billbear.web.errors import register_error_handlers
    from billbear.web.hooks import register_request_hooks
    from billbear.web.observability import init_sentry
    from billbear.web.session import get_secret_key

    # Error tracking: on only when SENTRY_DSN is set (web/observability.py).
    init_sentry(config.on_vercel())

    app = Flask(__name__, template_folder=str(config.TEMPLATE_DIR), static_folder=str(config.STATIC_DIR))
    if config.trust_proxy():
        # Behind Vercel / ngrok, TLS ends at the proxy. Trust its X-Forwarded-Proto/Host
        # so request.host_url (same-origin check, QR links) is the public https URL.
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    if config.on_vercel():
        # Vercel's Python runtime hands over the path still percent-encoded
        # ("/room/AB12CD/user/Guest%203"), where WSGI expects it decoded. Every route
        # with a person's name in it then saw "Guest%203", never matched the session's
        # "Guest 3", and redirected to itself forever: anyone with a space or a
        # non-English letter in their name was locked out.
        app.wsgi_app = DecodedPathInfo(app.wsgi_app)
    app.config["SECRET_KEY"] = get_secret_key()  # raises on Vercel if missing/weak
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_CONTENT_LENGTH
    db.init_app(app)

    @app.template_filter("money")
    def _money_filter(value, code=None, pdf=False):
        """{{ 1234.5 | money('INR') }} -> ₹1,234.50; with pdf=True, Rs. 1,234.50."""
        return format_money(value, get_currency(code), pdf=pdf)

    @app.template_filter("amount")
    def _amount_filter(value, code=None):
        """The number alone in a currency: {{ 1200 | amount('JPY') }} -> 1,200."""
        return format_amount(value, get_currency(code))

    app.add_template_filter(pdf_text, "pdf_text")

    @app.context_processor
    def _currency_context():
        return {
            "CURRENCY_CHOICES": currency_choices(),
            "MAX_CHARGES": MAX_CHARGES,
            "MIN_PEOPLE": MIN_PEOPLE,
            "MAX_PEOPLE": MAX_PEOPLE,
        }

    register_request_hooks(app)
    register_error_handlers(app)
    for blueprint in BLUEPRINTS:
        app.register_blueprint(blueprint)
    return app
