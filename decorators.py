"""
decorators.py — LinkPeek billing meter (free-tier autonomous business).

═══════════════════════════════════════════════════════════════════════════
SELF-SERVE PAYMENT FLOW — operator quick guide (read this first)
═══════════════════════════════════════════════════════════════════════════
Live end-to-end path (verified 2026-08-07):

  Buyer hits index.html  ->  fills "you@email.com"  ->  JS calls
  GET /api/subscribe?email=…  -> subscribe() issues a NON-EXPIRING Pro API
  key (lp_pro_…), saves it to keys.json, and returns a JSON pay_url:
     https://paypal.me/linkpeekpro/5.00   (pay_method="paypal")

  Buyer clicks pay_url  ->  PayPal.me page opens with amount pre-filled
  ->  buyer pays with card/PayPal balance  ->  operator's PayPal account
  gets the $5 minus PayPal fee, with the buyer's email visible in the
  notification.

  CRITICAL: pay_method priority in subscribe() is
     1. NowPayments crypto   (LINKPEEK_NOWPAYMENTS_KEY set  → crypto invoice)
     2. Stripe Payment Link  (LINKPEEK_STRIPE_LINK set       → hosted checkout)
     3. PayPal.me            (LINKPEEK_PAYPAL_ME set         → paypal.me/…/5.00)
     4. mailto fallback      (no env set)
  Currently only #3 is live (LINKPEEK_PAYPAL_ME=https://paypal.me/linkpeekpro).
  The PayPal.me handle "linkpeekpro" is verified-resolving (HTTP 200, →
  paypal.com/paypalme/linkpeekpro). The landing-page button (index.html
  #pp-paypal) was a REPLACE_HANDLE placeholder; now points at the same URL.

  QUOTA GATES ON paid==true (fixed 2026-08-07 — was a revenue leak):
     - A Pro key is issued at signup but starts at the FREE daily limit
       (100/day) until the operator flips paid:false → paid:true.
     - Trial keys (14-day) inherit Pro limits until they expire; they never
       carry a `paid` flag and are not affected by reconciliation.
     - This closes the leak where all 19 pro keys in keys.json were
       marked paid:false yet enjoyed the full 50,000/day Pro quota.

  RECONCILIATION (cron can't do this — human does):
     - PayPal notifies you per transaction; the buyer's email is your key.
     - Match the email to a key in keys.json  (grep keys.json for the email).
     - Flip that key's `paid: false` → `paid: true` to ACTIVATE Pro quota.
       Until then the key works, but only at the Free 100/day limit.

  UPGRADE PATHS (env knobs, all $0 fixed cost):
     LINKPEEK_NOWPAYMENTS_KEY   → crypto accepted (USDC/ETH/BTC, +60 coins)
     LINKPEEK_STRIPE_LINK       → card checkout (free Payment Link)
     LINKPEEK_BMC / _KOFI / _GH_SPONSORS → /api/donate tip channels
     LINKPEEK_PRO_PRICE         → dollar amount (default 5)
═══════════════════════════════════════════════════════════════════════════

Tier model (matches the live product site):
    - Free    : 100 requests/day, no key.  Metered per remote IP.
    - Pro/$5  : 50,000 requests/day, API key required.  Key-tier wins over IP tier.
                Quota only unlocks when the key's `paid` flag is true; an
                unpaid Pro key is held at the Free limit (effectively Free)
                until reconciliation.
    - Trial   : 14-day trial key, treated as Pro until expiry (never carries a
                `paid` flag).

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

# NowPayments crypto checkout — fully programmatic; the /v1/invoice endpoint
# accepts a plaintext API key via `x-api-key` and returns a hosted invoice URL.
# Operator only generates an API key once on https://nowpayments.io (no bank
# account, no KYC for the key) and pastes it here. Buyers pay in USDC/ETH/BTC
# and NowPayments fires an IPN callback — no human per-buyer setup.
NOWPAYMENTS_API_KEY = os.environ.get("LINKPEEK_NOWPAYMENTS_KEY", "").strip()
NOWPAYMENTS_API = os.environ.get(
    "LINKPEEK_NOWPAYMENTS_API", "https://api.nowpayments.io/v1"
)
# Public callback URL the operator sets so NowPayments posts IPN to our gate.
# If unset we still issue the invoice (NowPayments will just not callback).
NOWPAYMENTS_IPN_URL = os.environ.get("LINKPEEK_NOWPAYMENTS_IPN", "").strip()


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


def _nowpayments_invoice(email: str, key: str) -> dict:
    """Create a NowPayments hosted invoice for one Pro signup.

    Returns a dict {invoice_url, invoice_id, pay_address} on success, or an
    empty dict on any HTTP/JSON error (caller falls through). Uses only the
    stdlib via urllib so no new deps. The path /v1/invoice is public and was
    confirmed live against api.nowpayments.io (2026-08)."""
    import urllib.request
    import secrets as _s

    order_id = "lp_{}_{}".format(_utc_day(), _s.token_hex(6))
    payload = {
        "price_amount": PRO_PRICE_USD,
        "price_currency": "usd",
        "order_id": order_id,
        "order_description": "LinkPeek Pro - {} - monthly".format(email),
        "ipn_callback_url": NOWPAYMENTS_IPN_URL or "https://example.com/ipn",
        "success_url": "https://linkpeek.local/pro?ok=1&key={}".format(key),
        "cancel_url": "https://linkpeek.local/",
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        NOWPAYMENTS_API + "/invoice",
        data=data,
        headers={
            "x-api-key": NOWPAYMENTS_API_KEY,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", "replace")
        parsed = json.loads(body) if body else {}
        return {
            "invoice_url": parsed.get("invoice_url", "") or parsed.get("id", ""),
            "invoice_id": parsed.get("id", ""),
            "pay_address": parsed.get("pay_address", ""),
            "raw": parsed,
        }
    except Exception:
        return {}  # never let payment creation break the signup response


def subscribe(email: str, host: str = "") -> dict:
    """Self-serve Pro signup. Issues a Pro key and returns a payment link.

    Returns a dict with: email, api_key, plan, price_usd, pay_url, pay_method,
    instructions. The pay_url is whichever payment primitive the operator has
    configured. Priority: NowPayments crypto (programmatic), Stripe Payment
    Link, PayPal Me, then mailto fallback. If none is set, we fall back to a
    mailto: link asking the user to email the operator — still a working,
    $0-cost path that needs no third-party account."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("valid email required")

    api_key = issue_pro_key(email)

    # ── Idempotency/correctness fix ────────────────────────────────
    # The basic minting is already idempotent (issue_pro_key returns the
    # same key for a re-subscribe), but the response payload used to be
    # hardcoded as ``paid: False, status: pending_payment`` regardless of
    # the real stored state. That meant a user whose key had already been
    # auto-activated by the PayPal webhook (paid=True) was told on every
    # re-subscribe to pay again — a confusing, incorrect response. Look up
    # the live metadata for the returned key and reflect the real paid
    # state, current_effective_limit, and next_steps here.
    keys = _load_keys()
    stored = keys.get(api_key, {}) or {}
    already_paid = bool(stored.get("paid"))
    eff_limit = PRO_DAILY_LIMIT if already_paid else FREE_DAILY_LIMIT

    pay_url = ""
    pay_method = ""
    pay_meta = {}
    # When the key is already paid there is nothing to bill; surface an empty
    # pay_url instead of re-generating a payment link that would re-charge an
    # already-active customer. The next_steps block below branches on this too.
    if not already_paid:
        if NOWPAYMENTS_API_KEY:
            inv = _nowpayments_invoice(email, api_key)
            if inv.get("invoice_url"):
                pay_url = str(inv["invoice_url"])
                pay_method = "nowpayments_crypto"
                pay_meta = {
                    "invoice_id": inv.get("invoice_id", ""),
                    "pay_address": inv.get("pay_address", ""),
                    "accepted_coins": "USDC (Base), ETH, BTC, USDT, and 60+",
                }

        # Separate chain (not elif on the NowPayments block) so that a failed
        # NowPayments invoice — key set but API down — still falls through to a
        # working pay_url instead of returning an empty one.
        if not pay_url and STRIPE_LINK:
            # Stripe Payment Link: append the customer email so reconciliation is automatic.
            sep = "&" if "?" in STRIPE_LINK else "?"
            pay_url = "{}{}prefilled_email={}".format(STRIPE_LINK, sep, urllib.parse.quote(email))
            pay_method = "stripe"
        elif not pay_url and PAYPAL_ME:
            # PayPal Me: amount goes in the path, email shows up in the notification
            # the operator receives — they match it against the subscribed email.
            pay_url = "{}/{:.2f}".format(PAYPAL_ME, PRO_PRICE_USD)
            pay_method = "paypal"
        elif not pay_url:
            # Last-resort $0 path: a mailto asking the buyer to email the operator.
            pay_url = (
                "mailto:linkpeek@localhost?subject=LinkPeek%20Pro%20${:.0f}/mo"
                "&body=Email%3A%20{}%0AKey%3A%20{}%0APlease%20send%20payment%20instructions.".format(
                    PRO_PRICE_USD, urllib.parse.quote(email), api_key
                )
            )
            pay_method = "manual_email"

    # The Pro key is issued immediately and works right away, but at the
    # FREE 100/day limit. Pro quota (50k/day) only unlocks after the
    # operator flips paid:true (matching the buyer's email to a payment
    # notification). For an already-paid key (e.g. a re-subscribe after a
    # webhook activation) we reflect the live paid:true + active state so the
    # caller is not asked to pay again.  This is the idempotency fix that was
    # missing: the key was reused correctly, but the payload lied about paid.
    if already_paid:
        next_steps = [
            "1. Your Pro key is already ACTIVE with {:,} requests/day.".format(PRO_DAILY_LIMIT),
            "2. Payment was confirmed for this account — no further action needed.",
            "3. Check quota any time at GET /api/validate-key?key={}.".format(api_key),
        ]
        status = "active"
    else:
        next_steps = [
            "1. Your Pro key is live NOW but held at {} requests/day until payment is confirmed.".format(FREE_DAILY_LIMIT),
            "2. Open pay_url and pay ${:.0f}. PayPal notifies the operator (keyed on your email).".format(PRO_PRICE_USD),
            "3. Once reconciled, your key jumps to {:,} requests/day — no re-issue needed.".format(PRO_DAILY_LIMIT),
            "4. Questions? Your key status is always at GET /api/validate-key?key={}.".format(api_key),
        ]
        status = "pending_payment"
    return {
        "email": email,
        "api_key": api_key,
        "plan": "pro",
        "status": status,
        "paid": already_paid,
        "daily_limit": PRO_DAILY_LIMIT,
        "current_effective_limit": eff_limit,
        "price_usd": PRO_PRICE_USD,
        "billing_cycle": "month",
        "currency": "USD",
        "pay_url": pay_url,
        "pay_method": pay_method,
        "pay_meta": pay_meta,  # invoice_id, pay_address, accepted_coins (crypto)
        "next_steps": next_steps,
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


def _effective_plan(info: dict | None) -> str:
    """The tier actually enforced for quota purposes.

    Rules (single source of truth — used by both key_status and rate_limit):
      * No key / unknown key          → "free"   (100/day, per-IP metering)
      * plan == "trial"               → "pro"   (50k/day until it expires;
                                                 trials never carry `paid`)
      * plan == "pro" and paid is True → "pro"   (50k/day, reconciled)
      * plan == "pro" and paid is False → "free" (100/day — paywall held
                                                 until the operator flips
                                                 paid:true after payment)

    This is the gate that closes the revenue leak: a self-serve Pro key used
    to inherit full Pro quota at signup with paid:false, so nothing forced a
    buyer to actually pay."""
    if not info:
        return "free"
    plan = info.get("plan", "free")
    if plan == "trial":
        return "pro"
    if plan == "pro":
        return "pro" if info.get("paid") else "free"
    return "free"


def _plan_limit(plan: str) -> int:
    """Daily limit for an effective plan name."""
    return PRO_DAILY_LIMIT if plan == "pro" else FREE_DAILY_LIMIT


def key_status(apikey: str) -> dict | None:
    """Public-facing key info for /api/validate-key. Returns plan, limits,
    expiry, and used-today count, or None when the key is unknown/expired.
    Does NOT echo the email back (PII guard).

    ``plan`` reflects the stored plan (pro/trial/free); ``effective_plan`` is
    the tier actually enforced for quota. They differ only for an unpaid Pro
    key, whose effective_plan is "free" until paid:true."""
    info = _key_info(apikey)
    if not info:
        return None
    stored_plan = info.get("plan", "free")
    eff = _effective_plan(info)
    limit = _plan_limit(eff)
    used = used_today("key:" + apikey)
    return {
        "plan": stored_plan,
        "effective_plan": eff,
        "paid": bool(info.get("paid")),
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

            # Effective plan decides quota. An unpaid Pro key is held at the
            # Free limit (revenue gate) but is STILL metered on its own
            # "key:<apikey>" bucket so a signed-in unpaid user doesn't share
            # the per-IP free allowance with everyone on their NAT.
            eff = _effective_plan(info)
            if eff == "pro":
                plan = "pro"
                limit = PRO_DAILY_LIMIT
                meter_key = "key:" + apikey
            elif info:  # unpaid Pro key → Free limit, key-scoped meter
                plan = "free"
                limit = FREE_DAILY_LIMIT
                meter_key = "key:" + apikey
            else:  # no key — anonymous free, per-IP bucket
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
