"""Operations: health check, dataset provenance, published analysis results,
and serving the website itself from the same process."""
import json
import os
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, send_from_directory

from .. import config, db
from ..web import ApiError, cached

bp = Blueprint("ops", __name__)

RESULTS_DIR = os.path.join(config.ROOT, "data", "results")
SAMPLES_DIR = os.path.join(config.ROOT, "assets", "data")
# name in the URL -> (results file written by a script, bundled sample file)
RESULT_FILES = {
    "benchmark":      ("benchmark.json",      "sample_benchmark.json"),
    "geo-validation": ("geo_validation.json", "sample_geo_validation.json"),
    "drivers":        ("drivers.json",        "sample_drivers.json"),
    # No bundled sample: invented model accuracy would be misleading. 404 until the script has run.
    "amenity-model":  ("amenity_model.json",  None),
}


@bp.route("/api/health")
def health():
    """Liveness + dependency check. 200 when Postgres answers, 503 otherwise.
    Mongo is reported too, but only Postgres gates the status."""
    checks = {}
    try:
        db.q("SELECT 1")
        checks["postgres"] = "ok"
    except Exception as e:
        current_app.logger.warning("health: postgres check failed: %s", e)
        checks["postgres"] = "down"
    try:
        db.mongo().client.admin.command("ping")
        checks["mongo"] = "ok"
    except Exception as e:
        current_app.logger.warning("health: mongo check failed: %s", e)
        checks["mongo"] = "down"

    healthy = checks["postgres"] == "ok"
    resp = jsonify({
        "status": "ok" if healthy else "degraded",
        "checks": checks,
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    resp.status_code = 200 if healthy else 503
    resp.headers["Cache-Control"] = "no-store"
    return resp


@bp.route("/api/meta")
def meta():
    """Dataset provenance for the footer and About page (cached for an hour)."""
    def load():
        latest   = db.q("SELECT MAX(month) AS m FROM resale_transactions")[0]["m"]
        earliest = db.q("SELECT MIN(month) AS m FROM resale_transactions")[0]["m"]
        total    = db.q("SELECT COUNT(*)::bigint AS n FROM resale_transactions")[0]["n"]
        return {
            "earliest_month": earliest.isoformat() if earliest else None,
            "latest_month": latest.isoformat() if latest else None,
            "record_count": int(total),
            "source": "HDB Resale Flat Prices, data.gov.sg",
        }
    return jsonify(cached("meta", 3600, load))


@bp.route("/api/results/<name>")
def results(name):
    """Published analysis output (benchmark, geospatial validation, price
    drivers). Serves the file the scripts wrote to data/results/, or the
    bundled sample marked "sample": true so the UI can label it honestly."""
    if name not in RESULT_FILES:
        raise ApiError("Unknown result set.", 404, "not_found")
    real, sample = RESULT_FILES[name]
    for folder, fname, is_sample in ((RESULTS_DIR, real, False), (SAMPLES_DIR, sample, True)):
        if not fname:
            continue
        path = os.path.join(folder, fname)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                body = json.load(fh)
            body["sample"] = is_sample
            resp = jsonify(body)
            resp.headers["Cache-Control"] = "no-cache"
            return resp
    raise ApiError("No results yet: run the script that produces them (see USER_MANUAL.md).",
                   404, "not_found")


# ---------------------------------------------------------------------------
# Serving the website (one-service deployments and local runs)
# ---------------------------------------------------------------------------
# Only *.html in the project root and /assets/ are served; Python files,
# .env, data/ and tests never are.
@bp.route("/")
def site_index():
    return send_from_directory(config.ROOT, "index.html")


@bp.route("/<name>.html")
def site_page(name):
    # A missing page raises NotFound, which the error handler renders as 404.html.
    return send_from_directory(config.ROOT, name + ".html")


@bp.route("/assets/<path:filename>")
def site_asset(filename):
    return send_from_directory(os.path.join(config.ROOT, "assets"), filename)
