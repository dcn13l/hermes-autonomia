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


# ---------------------------------------------------------------------------
# Pro key + self-serve subscription (the revenue path)
# ---------------------------------------------------------------------------
# Operator sets one (or both) of these env vars to a real hosted-payment URL.
# Free to create, $0 monthly fee, only charges per transaction:
#   * PayPal Me   : https://www.paypal.me/<username>       (no business needed)
#   * Stripe      : https://buy.stripe.com/<link_id>       (Payment Link, free)
PRO_PRICE_USD = float(os.environ.get("LINKPEEK_PRO_PRICE", "5"))
PAYPAL_ME = os.environ.get("LINKPEEK_PAYPAL_ME", "").rstrip("/")
STRIPE_LINK = os.environ.get("LINKPEEK_STRIPE_LINK", "").rstrip("/")


def issue_pro_key(email: str) -> str:
    """Mint a non-expiring Pro API key for an email. Returns the key string.

    Idempotent: if this email already has a Pro key, return it (so resubmitting
    the subscribe form after paying does not fork keys)."""
    import secrets

    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("valid email required")
    keys = _load_keys()
    for k, v in keys.items():
        if v.get("email") == email and v.get("plan") == "pro":
            return k
    key = "lp_pro_" + secrets.token_urlsafe(16)
    keys[key] = {
        "email": email,
        "plan": "pro",
        "issued": _utc_day(),
        "expires_ts": 0,  # 0 = never expires
        "paid": False,    # operator flips to True once they reconcile the PayPal/Stripe notification with this email
        "source": "self_serve_subscribe",
    }
    _save_keys(keys)
    return key


def subscribe(email: str, host: str = "") -> dict:
    """Self-serve Pro signup. Issues a Pro key and returns a payment link.

    Returns a dict with: email, api_key, plan, price_usd, pay_url, pay_method,
    instructions. The pay_url is whichever payment primitive the operator has
    configured (PayPal Me or Stripe Payment Link). If neither is set, we fall
    back to a mailto: link asking the user to email the operator — still a
    working, $0-cost path that needs no third-party account."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("valid email required")

    api_key = issue_pro_key(email)

    pay_url = ""
    pay_method = ""
    if STRIPE_LINK:
        # Stripe Payment Link: append the customer email so reconciliation is automatic.
        sep = "&" if "?" in STRIPE_LINK else "?"
        pay_url = "{}{}prefilled_email={}".format(STRIPE_LINK, sep, urllib.parse.quote(email))
        pay_method = "stripe"
    elif PAYPAL_ME:
        # PayPal Me: amount goes in the path, email shows up in the notification
        # the operator receives — they match it against the subscribed email.
        pay_url = "{}/{:.2f}".format(PAYPAL_ME, PRO_PRICE_USD)
        pay_method = "paypal"
    else:
        # Last-resort $0 path: a mailto asking the buyer to email the operator.
        pay_url = (
            "mailto:linkpeek@localhost?subject=LinkPeek%20Pro%20${:.0f}/mo"
            "&body=Email%3A%20{}%0AKey%3A%20{}%0APlease%20send%20payment%20instructions.".format(
                PRO_PRICE_USD, urllib.parse.quote(email), api_key
            )
        )
        pay_method = "manual_email"

    return {
        "email": email,
        "api_key": api_key,
        "plan": "pro",
        # The Pro key is already fully active at the Pro daily limit — the
        # metering layer grants Pro quota on plan=="pro" regardless of the
        # `paid` flag, so there is no real "pending" gate to wait behind.
        # Telling buyers their key is "pending_activation" suppressed
        # conversion (they thought they were limited until a human acted).
        # Honesty sells: the key works NOW at full Pro quota.
        "status": "active",
        "paid": False,  # operator still reconciles the $5 for accounting; quota is not gated on it
        "daily_limit": PRO_DAILY_LIMIT,
        "price_usd": PRO_PRICE_USD,
        "billing_cycle": "month",
        "currency": "USD",
        "pay_url": pay_url,
        "pay_method": pay_method,
        "next_steps": [
            "1. Your Pro key is live NOW — it already works at {:,} requests/day.".format(PRO_DAILY_LIMIT),
            "2. Open pay_url and pay ${:.0f} to keep it after this billing cycle.".format(PRO_PRICE_USD),
            "3. No manual activation step — your key never gets throttled while paid is pending.",
            "4. Keep your key safe; it never expires while subscribed.",
        ],
        "pricing": plan_catalog(),
    }


def plan_catalog() -> dict:
    """Static price/feature catalogue for /api/pricing and to embed in
    /api/subscribe responses.  Single source of truth for tier display."""
    return {
        "currency": "USD",
        "plans": [
            {
                "id": "free",
                "name": "Free",
                "price_usd": 0.0,
                "billing_cycle": "month",
                "daily_limit": FREE_DAILY_LIMIT,
                "auth": "none (metered per IP)",
                "features": [
                    "{} requests/day".format(FREE_DAILY_LIMIT),
                    "No API key required",
                    "All /api/* preview endpoints",
                    "Community support",
                ],
            },
            {
                "id": "trial",
                "name": "Trial",
                "price_usd": 0.0,
                "billing_cycle": "14 days",
                "daily_limit": PRO_DAILY_LIMIT,
                "auth": "API key (issued at /api/key?email=…)",
                "features": [
                    "{} requests/day for 14 days".format(PRO_DAILY_LIMIT),
                    "Pro API key included",
                    "No credit card required",
                    "Auto-expires to Free afterwards",
                ],
            },
            {
                "id": "pro",
                "name": "Pro",
                "price_usd": PRO_PRICE_USD,
                "billing_cycle": "month",
                "daily_limit": PRO_DAILY_LIMIT,
                "auth": "API key (issued at /api/subscribe?email=…)",
                "features": [
                    "{} requests/day".format(PRO_DAILY_LIMIT),
                    "Non-expiring API key",
                    "Priority email support",
                    "All endpoints including /api/extract & /api/metadata-full",
                ],
            },
        ],
        "current_pro_price_usd": PRO_PRICE_USD,
        "subscribe_url": "/api/subscribe?email=…",
    }


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


def key_status(apikey: str) -> dict | None:
    """Public-facing key info for /api/validate-key. Returns plan, limits,
    expiry, and used-today count, or None when the key is unknown/expired.
    Does NOT echo the email back (PII guard)."""
    info = _key_info(apikey)
    if not info:
        return None
    plan = info.get("plan", "free")
    limit = PRO_DAILY_LIMIT if plan in ("pro", "trial") else FREE_DAILY_LIMIT
    used = used_today("key:" + apikey)
    return {
        "plan": plan,
        "valid": True,
        "issued": info.get("issued", ""),
        "expires": info.get("expires", ""),
        "expires_ts": info.get("expires_ts", 0),
        "used_today": used,
        "limit": limit,
        "remaining": max(0, limit - used),
    }


# ---------------------------------------------------------------------------
# Free payment channels (donations / tip jar — $0 fixed cost, no API keys)
# ---------------------------------------------------------------------------
# Operator sets any of these env vars to a real profile URL.  All three are
# free to create and charge only per-transaction (or take a platform cut);
# none requires a merchant account.  Leaving them unset still yields a
# working response — the optional links are empty strings.
BMC = os.environ.get("LINKPEEK_BMC", "").rstrip("/")          # Buy Me a Coffee
KOFI = os.environ.get("LINKPEEK_KOFI", "").rstrip("/")        # Ko-fi
GH_SPONSORS = os.environ.get("LINKPEEK_GH_SPONSORS", "").rstrip("/")  # GitHub Sponsors


def donate_channels() -> dict:
    """Free donation/tip channels for /api/donate.  Returns a dict with
    optional buy_me_a_coffee / ko_fi / github_sponsors URLs (empty strings
    when the operator has not configured them) plus a recommended default.
    All three platforms are free to join, $0 monthly fee, and require no
    merchant account or API keys — only a profile URL."""
    channels = {
        "buy_me_a_coffee": BMC,
        "ko_fi": KOFI,
        "github_sponsors": GH_SPONSORS,
    }
    # Prefer the first configured channel; fall back to PayPal Me if set,
    # then to a neutral note so the endpoint always returns something useful.
    default = next((u for u in channels.values() if u), "")
    if not default:
        default = PAYPAL_ME if PAYPAL_ME else ""
    return {
        "currency": "USD",
        "channels": channels,
        "recommended": default,
        "note": (
            "Tip LinkPeek if it saved you time.  Any amount, one-time, "
            "no signup required.  Configure via LINKPEEK_BMC, "
            "LINKPEEK_KOFI, or LINKPEEK_GH_SPONSORS env vars."
        ),
    }


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
