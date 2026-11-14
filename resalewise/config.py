"""Configuration, read from the environment (and a local `.env` file).

Nothing secret lives in the source code. Copy `.env.example` to `.env`, fill
in the values, and every script (app, loaders, benchmark) picks them up.

Required:
    DATABASE_URL   PostgreSQL connection string
    MONGO_URI      MongoDB connection string
    TOKEN_SECRET   random string (32+ chars) used to sign login tokens
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read_env_text(path):
    """Read a settings file. Windows Notepad can save with a BOM or as UTF-16."""
    raw = open(path, "rb").read()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    return raw.decode("utf-8-sig", errors="replace")


def load_env_file():
    """Put KEY=VALUE lines from `.env` (or `.env.txt`, which Notepad creates when
    "Save as type" is not "All Files") into os.environ. Real environment
    variables always win. Returns the file name used, or None."""
    for name in (".env", ".env.txt"):
        path = os.path.join(ROOT, name)
        if not os.path.isfile(path):
            continue
        try:
            text = _read_env_text(path)
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if key.lower().startswith("set "):          # tolerate "set KEY=value"
                key = key[4:].strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            if key and key not in os.environ:
                os.environ[key] = val
        return name
    return None


load_env_file()

# Values that must never be used for a real deployment.
_PLACEHOLDER_SECRETS = {"", "change-me", "changeme", "resalewise-sg-dev-secret-change-me",
                        "replace-with-a-long-random-string"}


class ConfigError(RuntimeError):
    """Raised when a required setting is missing; the message says how to fix it."""


def _required(name, hint):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. {hint}\n"
            "Copy .env.example to .env in the project folder and fill it in "
            "(see USER_MANUAL.md, section 2).")
    return value


def database_url():
    """Postgres URL for psycopg2 (plain postgresql:// scheme)."""
    url = _required("DATABASE_URL", "It should look like postgresql://user:pass@host:5433/resalewise")
    for prefix in ("postgresql+psycopg2://", "postgres://"):
        if url.startswith(prefix):
            url = "postgresql://" + url[len(prefix):]
    return url


def sqlalchemy_url():
    """Same database, but with the driver named explicitly. SQLAlchemy 2.1
    changed the default driver for postgresql:// to psycopg (v3), which is not
    installed; naming psycopg2 keeps a fresh `pip install` working."""
    return "postgresql+psycopg2://" + database_url()[len("postgresql://"):]


def mongo_uri():
    return _required("MONGO_URI", "It should look like mongodb://localhost:27018")


def mongo_db_name():
    return os.environ.get("MONGO_DB", "hdb")


def token_secret():
    secret = os.environ.get("TOKEN_SECRET", "").strip()
    if secret in _PLACEHOLDER_SECRETS or len(secret) < 16:
        raise ConfigError(
            "TOKEN_SECRET is missing or too short (needs 16+ characters, never the example value).\n"
            "Generate one with:  python -c \"import secrets; print(secrets.token_urlsafe(32))\"\n"
            "and put it in your .env file.")
    return secret.encode()


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def check_or_exit():
    """Fail fast with a readable message instead of a stack trace."""
    try:
        database_url(); mongo_uri(); token_secret()
    except ConfigError as e:
        sys.stderr.write("\n[config] " + str(e) + "\n\n")
        sys.exit(2)
