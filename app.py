"""
Entry point. Vercel's Python runtime imports `app` from this file (vercel.json);
`python app.py` (or run.py) starts the local dev server.
"""
import os

from billbear import create_app

app = create_app()

if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    port = int(os.getenv("PORT", 5001))
    app.run(debug=debug, host="0.0.0.0", port=port)
