"""ResaleWise SG — Flask application factory.

    from resalewise import create_app
    app = create_app()

Layout
------
config.py        settings from the environment / .env (no secrets in code)
db.py            PostgreSQL pool + lazy MongoDB client
web.py           JSON error contract, rate limiting, cache, validation
auth.py          signed bearer tokens
benchmarks.py    "value" benchmarks: each sale vs its own period
mongo_setup.py   MongoDB validators and indexes
mongo_queries.py MongoDB equivalents of the SQL search filters
amenities.py    pure helpers for nearby places: scoring, GeoJSON/CSV parsing, engine comparison
admin.py        the admin area: console guards, the OLAP cube's SQL, terminal commands
concurrency.py  the admin area's concurrency runs: two transactions on real database sessions
routes/          one blueprint per area of the API
"""
import logging
import os

from flask import Flask
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

from . import web


def create_app():
    app = Flask(__name__, static_folder=None)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

    # Behind a reverse proxy (Render, Nginx...) the client IP arrives in
    # X-Forwarded-For. Set TRUST_PROXY=1 there so rate limiting sees real IPs.
    if os.environ.get("TRUST_PROXY") == "1":
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # CORS: open by default (the data is public). In production lock it to the
    # site's own origin(s) with CORS_ORIGINS="https://your-site.example".
    CORS(app, resources={r"/api/*": {
        "origins": [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",")],
        "expose_headers": ["Retry-After", "Content-Disposition", "Server-Timing"],
    }})

    web.register_error_handlers(app)
    web.register_request_hooks(app)

    from .routes import account, admin, amenities, bookmarks, drivers, engines, geo, insights, ops, reviews, search
    for module in (account, search, insights, bookmarks, reviews, geo, amenities, drivers, engines, admin, ops):
        app.register_blueprint(module.bp)
    return app
