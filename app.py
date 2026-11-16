"""ResaleWise SG — entry point.

    python app.py                 # development server on http://127.0.0.1:5000
    gunicorn app:app              # production (see render.yaml / Procfile)

The application itself lives in the `resalewise/` package; this file only
checks the configuration, runs the light start-up tasks and exposes `app`.
"""
import os
import threading

from resalewise import config, create_app, db

config.check_or_exit()          # clear message instead of a stack trace if .env is missing
app = create_app()


def _startup_tasks():
    """Idempotent, light-weight start-up work. Failures are logged, never fatal."""
    try:
        with db.pg_conn() as conn, conn.cursor() as cur:
            cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS name VARCHAR(80)")
    except Exception as e:
        app.logger.warning("[startup] users.name migration skipped: %s", e)
    # Pre-compute what the first visit to the home, search and insights pages
    # asks for, so no visitor (or a live demo) waits for a cold aggregate.
    # Answers are cached for a day; the cache keys ignore parameter order and
    # "+" vs "%20", so these match what the browser sends.
    try:
        c = app.test_client()
        for url in WARM_URLS:
            c.get(url)
        app.logger.info("[startup] cache warmed (%d requests)", len(WARM_URLS))
    except Exception as e:
        app.logger.warning("[startup] cache warm-up skipped: %s", e)


WARM_URLS = (
    # every page
    "/api/options",
    "/api/meta",
    # home page
    "/api/insights/trend?flat_type=3%20ROOM&months=240",
    "/api/insights/trend?flat_type=4%20ROOM&months=240",
    "/api/insights/trend?flat_type=5%20ROOM&months=240",
    "/api/insights/town-medians?flat_type=4%20ROOM&months=24",
    "/api/insights/summary?flat_type=4%20ROOM&months=24",
    "/api/town-cards",
    "/api/search?flat_type=4%20ROOM&limit=1",
    # search page, no filters
    "/api/search?limit=20&page=1",
    "/api/search-stats",
    # insights page defaults (4-room, all towns, last 24 months)
    "/api/insights/trend?flat_type=4%20ROOM&months=24",
    "/api/insights/by-flat-type?months=24",
    "/api/insights/price-bands?flat_type=4%20ROOM&months=24",
    "/api/insights/momentum?flat_type=4%20ROOM&min_n=30",
    "/api/insights/town-medians?flat_type=4%20ROOM&months=12",
)


if os.environ.get("RW_SKIP_STARTUP_MIGRATIONS") != "1":
    threading.Thread(target=_startup_tasks, daemon=True, name="startup").start()


if __name__ == "__main__":
    # The Werkzeug debugger allows code execution, so it is opt-in and the
    # server only listens on this machine.
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)),
            debug=os.environ.get("FLASK_DEBUG") == "1")
