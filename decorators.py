"""
decorators.py — LinkPeek billing meter (free-tier autonomous business).

Tier model (matches the live product site):
    - Free    : 100 requests/day, no key.  Metered per remote IP.
    - Pro/$5  : 50,000 requests/day, API key required.  Key-tier wins over IP tier.
    - Trial   : 14-day trial key, treated as Pro until expiry.

Metering key = ("ip:<addr>", "ip:<addr>" for the free tier) so a user on a
shared NAT still gets a fair per-IP allowance, and a key-holder is never
double-billed against the free bucket. Day-bucketed counters reset at UTC
midnight (the same epoch-day the /api/health endpoint reports).

This is a *stub*: counters live in-process (thread-safe dict). For real revenue
an operator of the autonomous business would swap the Counter for a SQL row or
a Redis hash keyed on the same meter_key. The public interface (rate_limit,
increment, remaining) is intentionally stable so that swap is a drop-in.

Stdlib only. No paid deps.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
from functools import wraps

# ---------------------------------------------------------------------------
# Configuration (env-overridable so the same file runs locally and in prod)
# ---------------------------------------------------------------------------
FREE_DAILY_LIMIT = int(os.environ.get("LINKPEEK_FREE_LIMIT", "100"))
PRO_DAILY_LIMIT = int(os.environ.get("LINKPEEK_PRO_LIMIT", "50000"))
TRIAL_DAYS = int(os.environ.get("LINKPEEK_TRIAL_DAYS", "14"))

# In-memory trial-key registry. In prod this would be a DB table; the stub
# persists to LINKPEEK_KEYS_FILE so trials survive restarts.
_KEYS_FILE = os.environ.get(
    "LINKPEEK_KEYS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "keys.json"),
)

# In-memory counter: {meter_key: {"day": "2026-08-05", "count": N}}
# meter_key is either "ip:<addr>" (free) or "key:<apikey>" (pro/trial).
# Thread-safe; one process. Multi-worker deployments swap for Redis/DB.
_USAGE: dict[str, dict] = {}
_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Day bucketing
# ---------------------------------------------------------------------------
def _utc_day() -> str:
    """UTC date string YYYY-MM-DD — matches the value /api/health reports."""
    return time.strftime("%Y-%m-%d", time.gmtime())


def _load_keys() -> dict:
    if not os.path.exists(_KEYS_FILE):
        return {}
    try:
        with open(_KEYS_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_keys(keys: dict) -> None:
    try:
        with open(_KEYS_FILE, "w", encoding="utf-8") as fh:
            json.dump(keys, fh, indent=2)
    except OSError:
        pass  # best-effort; in-memory copy is still the source of truth


def issue_trial_key(email: str) -> str:
    """Mint a 14-day trial API key for an email. Returns the key string."""
    import secrets

    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("valid email required")
    keys = _load_keys()
    # Reuse if this email already has an unexpired key.
    for k, v in keys.items():
        if v.get("email") == email and v.get("plan") == "trial":
            return k
    key = "lp_" + secrets.token_urlsafe(16)
    keys[key] = {
        "email": email,
        "plan": "trial",
        "issued": _utc_day(),
        "expires": _utc_day(),  # replaced below with expiry
        "expires_ts": int(time.time()) + TRIAL_DAYS * 86400,
    }
    keys[key]["expires"] = time.strftime(
        "%Y-%m-%d", time.gmtime(keys[key]["expires_ts"])
    )
    _save_keys(keys)
    return key


def _key_info(apikey: str) -> dict | None:
    if not apikey:
        return None
    keys = _load_keys()
    info = keys.get(apikey)
    if not info:
        return None
    if info.get("expires_ts") and time.time() > info["expires_ts"]:
        return None  # expired
    return info


def _client_ip(flask_request) -> str:
    """Best-effort client IP, honouring the first hop in X-Forwarded-For."""
    xff = flask_request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return flask_request.remote_addr or "0.0.0.0"


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------
def increment(meter_key: str, amount: int = 1) -> int:
    """Increment the daily counter for a meter key. Returns the new count."""
    today = _utc_day()
    with _LOCK:
        rec = _USAGE.get(meter_key)
        if not rec or rec.get("day") != today:
            rec = {"day": today, "count": 0}
            _USAGE[meter_key] = rec
        rec["count"] += amount
        return rec["count"]


def used_today(meter_key: str) -> int:
    today = _utc_day()
    with _LOCK:
        rec = _USAGE.get(meter_key)
        return rec["count"] if rec and rec.get("day") == today else 0


def daily_totals() -> dict:
    """Aggregate used-today across all meter keys — for /api/health."""
    today = _utc_day()
    total = 0
    with _LOCK:
        for rec in _USAGE.values():
            if rec.get("day") == today:
                total += rec["count"]
    return {"day": today, "count": total}


# ---------------------------------------------------------------------------
# The rate-limit decorator
# ---------------------------------------------------------------------------
def rate_limit(flask_app):
    """Decorator factory that meters and enforces per-day limits.

    Usage in app.py::

        from decorators import rate_limit
        @app.route("/api/preview")
        @rate_limit(app)
        def preview():
            ...

    The wrapped view receives `meter_key`, `plan`, `limit` and `quota_used`
    via `flask.g` so it can echo a `quota` object back to the caller (the live
    API does this). On limit exceeded it short-circuits to a 429 JSON response.
    """

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            from flask import g, jsonify, request  # local import keeps this file stdlib-only

            apikey = (request.values.get("key") or "").strip()
            ip = _client_ip(request)
            info = _key_info(apikey)

            if info:  # key wins: pro/trial bucket
                plan = "pro"
                limit = PRO_DAILY_LIMIT
                meter_key = "key:" + apikey
            else:  # free, per-IP bucket
                plan = "free"
                limit = FREE_DAILY_LIMIT
                meter_key = "ip:" + ip

            used = used_today(meter_key)
            g.meter_key = meter_key
            g.plan = plan
            g.limit = limit
            g.quota_used = used

            if used >= limit:
                jsonify  # noqa: silence linter
                resp = flask_app.make_response(
                    jsonify(
                        error="daily_limit_exceeded",
                        plan=plan,
                        quota={"used_today": used, "limit": limit},
                        upgrade_url="/",
                    )
                )
                resp.status_code = 429
                resp.headers["Retry-After"] = "86400"
                return resp

            # count the request *before* the view runs so a slow fetch
            # can't be double-counted by a retry, and so an exceeded
            # limit is visible on the *response* of the call that
            # crossed the line.
            increment(meter_key)
            g.quota_used = used + 1
            return view(*args, **kwargs)

        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Small convenience: build the quota echo block the live API returns.
# ---------------------------------------------------------------------------
def quota_echo(g):
    """Read flask.g and return the {used_today, limit} the API echoes."""
    return {
        "used_today": getattr(g, "quota_used", 0),
        "limit": getattr(g, "limit", FREE_DAILY_LIMIT),
    }


def record_billing(meter_key: str, plan: str, identity: str) -> None:
    """Accounting hook — write one line to LEDGER (markdown billing model).

    The autonomous business keeps a markdown ledger of billable events.
    This appends a single line per billable call; a downstream job totals it
    into invoice.md. Override LINKPEEK_LEDGER to point elsewhere.
    """
    ledger = os.environ.get(
        "LINKPEEK_LEDGER",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "ledger_billable.md"),
    )
    line = "| {} | {} | {} | {} |\n".format(_utc_day(), plan, meter_key, identity)
    try:
        with open(ledger, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass  # billing must never break a request
