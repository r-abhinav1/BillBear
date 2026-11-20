"""Static pages: home, help, terms, and the health check."""
from flask import Blueprint, current_app, jsonify, redirect, render_template, url_for

from billbear import db

bp = Blueprint("pages", __name__)


@bp.route("/")
def index():
    return render_template("index.html")


# Help: an index and one page per topic, read in this order.
HELP_TOPICS = [
    {"slug": "basics", "title": "The basics", "blurb": "From scan to split in five steps"},
    {"slug": "sharing", "title": "Shared or yours", "blurb": "When the bill has several of one dish"},
    {"slug": "tax", "title": "Tax and service", "blurb": "Who pays how much of the charges"},
    {"slug": "currency", "title": "Other currencies", "blurb": "Bills from abroad, and why nothing is converted"},
    {"slug": "host", "title": "For the host", "blurb": "Everything you can do in the room"},
    {"slug": "scan", "title": "The scan", "blurb": "How Bear reads the bill, and where it slips"},
]


@bp.route("/help")
def help_index():
    return render_template("help/index.html", topics=HELP_TOPICS)


@bp.route("/help/<topic>")
def help_topic(topic):
    index = next((i for i, t in enumerate(HELP_TOPICS) if t["slug"] == topic), None)
    if index is None:
        return redirect(url_for("pages.help_index"))
    return render_template(
        f"help/{HELP_TOPICS[index]['slug']}.html",
        topics=HELP_TOPICS,
        topic=HELP_TOPICS[index],
        position=index + 1,
        next_topic=HELP_TOPICS[index + 1] if index + 1 < len(HELP_TOPICS) else None,
    )


@bp.route("/about")
def about():
    # The About page was folded into Help.
    return redirect(url_for("pages.help_index"), code=301)


@bp.route("/terms")
def terms():
    return render_template("terms.html")


@bp.route("/healthz")
def healthz():
    """For an uptime monitor: 200 when the app can reach its database, 503 when not."""
    try:
        with db.get_conn().cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as exc:
        current_app.logger.warning("healthz: database check failed: %s", type(exc).__name__)
        response = jsonify({"status": "error", "database": "unreachable"})
        response.status_code = 503
    else:
        response = jsonify({"status": "ok", "database": "ok"})
    response.headers["Cache-Control"] = "no-store"
    return response
