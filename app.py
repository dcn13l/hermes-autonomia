#!/usr/bin/env python3
"""
LinkPeek — link preview API + QR code generator.

Single Flask app. Endpoints:
    GET  /                     homepage (serves ./index.html)
    GET  /api/preview          metered link-preview extraction
    GET  /api/extract          raw meta + links + headings (deeper crawl)
    GET  /api/metadata-full    full metadata dump (every meta tag)
    GET  /api/batch            up to 5 URLs at once (parallel fetch)
    GET  /api/screenshot-url-hint  returns a suggestion / strong hint for a
                                   screenshot service URL the caller can hit
    GET  /api/qr               generate QR code PNG from ?text=
    GET  /api/key?email=…      issues a 14-day trial API key
    GET  /api/health           {ok, today:{day, count}}
"""

from __future__ import annotations

import os
import re
import time
import socket
import ssl
import gzip
import zlib
import io
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, quote as urlquote, urldefrag
from urllib.request import Request, urlopen, build_opener, ProxyHandler
from urllib.error import URLError, HTTPError

from flask import Flask, jsonify, request, g, send_file, Response

from decorators import (
    rate_limit,
    quota_echo,
    issue_trial_key,
    daily_totals,
    record_billing,
    subscribe,
    key_status,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=BASE_DIR, template_folder=BASE_DIR)

# Maximum bytes of HTML we will pull into memory per request.
_MAX_BYTES = 2 * 1024 * 1024  # 2 MiB
# Batch endpoint cap.
_BATCH_MAX = 5
_BATCH_TIMEOUT = 12.0

# Service metadata for /api/status
__version__ = "1.1.0"
_START_TIME = time.time()


# ============================================================================
# stdlib-only link preview extraction
# ============================================================================
class _PeekParser(HTMLParser):
    """Extract title, meta tags, favicon, headings, and links from HTML.

    Parses the whole document (not just <head>) so that pages with malformed
    or missing </head> still yield a title and favicon. Heading and link
    collection continues into <body> for the /api/extract endpoint.
    """

    def __init__(self, base_url: str, collect_body: bool = False):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.collect_body = collect_body
        self._in_title = False
        self._head_over = False
        self._current_heading_level = 0
        self._current_heading_text = ""
        self._current_a_href = ""
        self._current_a_text = ""
        self._in_a = False
        self.title: str = ""
        self.meta: dict[str, str] = {}
        self.favicon: str = ""
        self.headings: list[dict] = []
        self.links: list[dict] = []

    def _stop_head(self):
        self._in_title = False
        self._head_over = True

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
            return
        if not self._head_over and tag == "meta":
            key = a.get("property") or a.get("name")
            if key:
                key = key.lower()
                if key not in self.meta and a.get("content"):
                    self.meta[key] = a["content"]
        if not self._head_over and tag == "link":
            rel = (a.get("rel") or "").lower()
            href = a.get("href")
            if href and "icon" in rel and not self.favicon:
                self.favicon = urljoin(self.base_url, href)
        if self.collect_body:
            if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                self._current_heading_level = int(tag[1])
                self._current_heading_text = ""
            if tag == "a":
                self._in_a = True
                self._current_a_href = a.get("href", "")
                self._current_a_text = ""

    def handle_endtag(self, tag):
        if tag == "head":
            self._stop_head()
        elif tag == "title":
            self._in_title = False
        if self.collect_body:
            if tag in ("h1", "h2", "h3", "h4", "h5", "h6") and self._current_heading_level:
                text = re.sub(r"\s+", " ", self._current_heading_text).strip()
                if text:
                    self.headings.append(
                        {"level": self._current_heading_level, "text": text}
                    )
                self._current_heading_level = 0
                self._current_heading_text = ""
            if tag == "a" and self._in_a:
                href = self._current_a_href.strip()
                text = re.sub(r"\s+", " ", self._current_a_text).strip()
                if href:
                    self.links.append(
                        {
                            "href": urljoin(self.base_url, href) if not href.startswith("#") else href,
                            "text": text[:200],
                        }
                    )
                self._in_a = False
                self._current_a_href = ""
                self._current_a_text = ""

    def handle_data(self, data):
        if self._in_title and not self._head_over:
            self.title += data
        if self.collect_body:
            if self._current_heading_level:
                self._current_heading_text += data
            if self._in_a:
                self._current_a_text += data


def _decode(resp_bytes: bytes, headers) -> str:
    data = resp_bytes
    enc = (headers.get("Content-Encoding") or "").lower()
    if enc == "gzip":
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError):
            pass
    elif enc == "deflate":
        try:
            data = zlib.decompress(data)
        except zlib.error:
            try:
                data = zlib.decompress(data, -zlib.MAX_WBITS)
            except zlib.error:
                pass
    elif enc == "br":
        # brotli is not stdlib; attempt import for environments that have it.
        try:
            import brotli  # type: ignore
            data = brotli.decompress(data)
        except (ImportError, Exception):
            pass
    charset = "utf-8"
    ctype = (headers.get("Content-Type") or "").lower()
    m = re.search(r"charset=([\w\-]+)", ctype)
    if m:
        charset = m.group(1)
    try:
        return data.decode(charset, errors="ignore")
    except LookupError:
        return data.decode("utf-8", errors="ignore")


def _fetch(url: str, timeout: float = 8.0) -> tuple[str, str, dict]:
    opener = build_opener(ProxyHandler())
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; LinkPeek/1.0; +https://github.com/linkpeek)",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        },
        method="GET",
    )
    try:
        resp = opener.open(req, timeout=timeout)
    except HTTPError as e:
        body = b""
        try:
            body = e.read(_MAX_BYTES)
        except (OSError, AttributeError):
            pass
        if body:
            headers = {"Content-Type": e.headers.get("Content-Type", "")}
            return (e.url or url, _decode(body, headers), headers)
        raise
    raw = resp.read(_MAX_BYTES)
    final_url = resp.geturl()
    headers = {k: v for k, v in resp.headers.items()}
    html_text = _decode(raw, headers)
    return final_url, html_text, headers


def _clean(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip()


def _normalize_url(url: str) -> str:
    if not url:
        raise ValueError("missing url")
    url = url.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", url):
        url = "https://" + url
    parts = urlsplit(url)
    if not parts.netloc:
        raise ValueError("invalid url")
    return url


def _extract_head(html_text: str) -> str:
    """Robustly extract everything up to </head> (case-insensitive)."""
    lower = html_text.lower()
    idx = lower.find("</head>")
    if idx != -1:
        return html_text[: idx + len("</head>")]
    # No </head> found — fall back to the leading 64 KiB.
    return html_text[:65536]


def preview_link(url: str, collect_body: bool = False) -> dict:
    url = _normalize_url(url)
    final_url, html_text, _ = _fetch(url)
    # Parse the head for title/meta/favicon, and optionally the full doc
    # for headings + links.
    head_html = _extract_head(html_text)
    parser = _PeekParser(final_url, collect_body=collect_body)
    try:
        parser.feed(head_html)
    except AssertionError:
        pass
    if collect_body:
        try:
            parser.feed(html_text[len(head_html) :])
        except AssertionError:
            pass
    title = _clean(parser.title)
    description = _clean(
        parser.meta.get("description")
        or parser.meta.get("og:description")
        or parser.meta.get("twitter:description")
    )
    og_title = _clean(parser.meta.get("og:title") or parser.meta.get("twitter:title"))
    if not title:
        title = og_title
    image = parser.meta.get("og:image") or parser.meta.get("twitter:image") or ""
    if image:
        image = urljoin(final_url, image)
    site_name = _clean(parser.meta.get("og:site_name")) or ""
    favicon = parser.favicon or ""
    if not favicon or favicon.startswith("data:"):
        parts = urlsplit(final_url)
        if parts.scheme and parts.netloc:
            favicon = "{}://{}/favicon.ico".format(parts.scheme, parts.netloc)
    result = {
        "url": final_url,
        "title": title,
        "description": description,
        "image": image,
        "site_name": site_name,
        "favicon": favicon,
    }
    if collect_body:
        result["headings"] = parser.headings[:50]
        result["links"] = parser.links[:100]
        result["meta"] = parser.meta
    return result


# ============================================================================
# Screenshot URL hint — suggests a public screenshot-as-a-service URL the
# caller can fetch themselves. We never make the outbound screenshot call
# (that needs a headless browser); we just hand back a ready-made URL.
# ============================================================================
def _screenshot_hint(url: str) -> dict:
    url = _normalize_url(url)
    # Encode the target URL for use as a path/query segment.
    encoded = urlquote(url, safe="")
    return {
        "target_url": url,
        "hint": (
            "Use a headless-browser screenshot service. Example ready-to-fetch URLs:"
        ),
        "suggestions": [
            {
                "service": "microlink.io (free tier)",
                "url": "https://api.microlink.io/?url={}&screenshot&meta=false".format(encoded),
            },
            {
                "service": "thum.io",
                "url": "https://image.thum.io/get/width/1200/crop/800/{}".format(encoded),
            },
            {
                "service": "webscreenshot",
                "url": "https://api.websiteplanet.com/v1/screenshot?url={}".format(encoded),
            },
        ],
        "note": "LinkPeek stays stdlib-only and does not render screenshots itself.",
    }


# ============================================================================
# QR Code endpoint — second micro-service
# ============================================================================
try:
    import qrcode
    from io import BytesIO

    @app.route("/api/qr")
    @rate_limit(app)
    def api_qr():
        text = (request.values.get("text") or "").strip()
        if not text:
            return jsonify(error="pass ?text=..."), 400
        qr = qrcode.QRCode(version=1, box_size=10, border=2)
        qr.add_data(text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return Response(buf.getvalue(), mimetype="image/png")
except ImportError:
    @app.route("/api/qr")
    def api_qr_unavailable():
        return jsonify(error="qrcode lib not installed"), 503


# ============================================================================
# Routes
# ============================================================================
@app.route("/")
def home():
    idx = os.path.join(BASE_DIR, "index.html")
    if os.path.exists(idx):
        return send_file(idx)
    return "<h1>LinkPeek</h1><p>API at <code>/api/preview?url=...</code></p>"


@app.route("/api/preview")
@rate_limit(app)
def api_preview():
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="pass ?url=https://..."), 400
    try:
        out = preview_link(url)
    except (URLError, HTTPError, socket.timeout, ValueError, ssl.SSLError) as e:
        return jsonify(url=url, error="fetch_failed: %s" % type(e).__name__), 502
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, url[:200])
    return jsonify(out)


@app.route("/api/extract")
@rate_limit(app)
def api_extract():
    """Deeper crawl: raw meta dict + up to 50 headings + 100 links."""
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="pass ?url=https://..."), 400
    try:
        out = preview_link(url, collect_body=True)
    except (URLError, HTTPError, socket.timeout, ValueError, ssl.SSLError) as e:
        return jsonify(url=url, error="fetch_failed: %s" % type(e).__name__), 502
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, url[:200])
    return jsonify(out)


@app.route("/api/metadata-full")
@rate_limit(app)
def api_metadata_full():
    """Return every meta tag found in the head, plus title and favicon."""
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="pass ?url=https://..."), 400
    try:
        url = _normalize_url(url)
        final_url, html_text, headers = _fetch(url)
    except (URLError, HTTPError, socket.timeout, ValueError, ssl.SSLError) as e:
        return jsonify(url=url, error="fetch_failed: %s" % type(e).__name__), 502
    head_html = _extract_head(html_text)
    parser = _PeekParser(final_url)
    try:
        parser.feed(head_html)
    except AssertionError:
        pass
    out = {
        "url": final_url,
        "title": _clean(parser.title),
        "favicon": parser.favicon,
        "meta": parser.meta,
        "response_headers": dict(headers),
        "head_html_length": len(head_html),
    }
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, url[:200])
    return jsonify(out)


@app.route("/api/screenshot-url-hint")
@rate_limit(app)
def api_screenshot_hint():
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="pass ?url=https://..."), 400
    try:
        out = _screenshot_hint(url)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, url[:200])
    return jsonify(out)


@app.route("/api/batch")
@rate_limit(app)
def api_batch():
    """Fetch up to 5 URLs at once. Accepts ?url= repeated or ?urls=a,b,c."""
    urls = request.values.getlist("url")
    csv = (request.values.get("urls") or "").strip()
    if csv:
        urls.extend(u.strip() for u in csv.split(",") if u.strip())
    urls = list(dict.fromkeys(u.strip() for u in urls if u.strip()))  # de-dup, keep order
    if not urls:
        return jsonify(error="pass ?url=... (up to 5)"), 400
    if len(urls) > _BATCH_MAX:
        return jsonify(error="batch_limit_exceeded", max=_BATCH_MAX, got=len(urls)), 400

    def _one(u):
        try:
            return preview_link(u)
        except Exception as e:
            return {"url": u, "error": "fetch_failed: %s" % type(e).__name__}

    results_by_url = {u: {"url": u, "error": "timeout: batch"} for u in urls}
    with ThreadPoolExecutor(max_workers=min(len(urls), 5)) as ex:
        future_map = {ex.submit(_one, u): u for u in urls}
        try:
            for fut in as_completed(future_map, timeout=_BATCH_TIMEOUT):
                u = future_map[fut]
                try:
                    results_by_url[u] = fut.result(timeout=_BATCH_TIMEOUT)
                except Exception as e:
                    results_by_url[u] = {"url": u, "error": "timeout: %s" % type(e).__name__}
        except TimeoutError:
            # as_completed's own timeout fired: anything not yet delivered
            # stays at the "timeout: batch" placeholder set above, and we
            # cancel whatever futures are still running so the pool exits.
            for fut, u in future_map.items():
                if results_by_url[u].get("error", "").startswith("timeout"):
                    fut.cancel()

    # Preserve the original request order (matches deduped `urls`).
    results = [results_by_url[u] for u in urls]

    record_billing(g.meter_key, g.plan, "batch:%d" % len(urls))
    out = {
        "count": len(urls),
        "results": results,
        "quota": quota_echo(g),
    }
    return jsonify(out)


@app.route("/api/key")
def api_key():
    email = (request.values.get("email") or "").strip()
    try:
        key = issue_trial_key(email)
    except ValueError as ve:
        return jsonify(error=str(ve)), 400
    return jsonify(key=key, trial_days=14, note="use ?key=<key> on /api/preview")


@app.route("/api/subscribe")
def api_subscribe():
    """Self-serve Pro signup — issues a Pro API key and returns a payment link."""
    email = (request.values.get("email") or "").strip()
    try:
        result = subscribe(email)
    except ValueError as ve:
        return jsonify(error=str(ve)), 400
    return jsonify(result)


# ============================================================================
# New endpoints: strict OpenGraph, URL diff, key validation, service status
# ============================================================================
def _og_only(preview: dict) -> dict:
    """Reduce a preview_link() result to OpenGraph fields, camelCased,
    matching what most embed/link-card consumers expect."""
    src = preview or {}
    return {
        "url": src.get("url", ""),
        "title": src.get("title", ""),
        "description": src.get("description", ""),
        "image": src.get("image", ""),
        "siteName": src.get("site_name", ""),
    }


@app.route("/api/opengraph")
@rate_limit(app)
def api_opengraph():
    """Strict OpenGraph view — only og:* derived fields, camelCased JSON."""
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="pass ?url=https://..."), 400
    try:
        out = preview_link(url)
    except (URLError, HTTPError, socket.timeout, ValueError, ssl.SSLError) as e:
        return jsonify(url=url, error="fetch_failed: %s" % type(e).__name__), 502
    out = _og_only(out)
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, url[:200])
    return jsonify(out)


@app.route("/api/diff")
@rate_limit(app)
def api_diff():
    """Compare two URLs' link-preview metadata and report field-level diffs.

    Query: ?url1=...&url2=...  (both required, fetched in parallel)."""
    u1 = (request.values.get("url1") or "").strip()
    u2 = (request.values.get("url2") or "").strip()
    if not u1 or not u2:
        return jsonify(error="pass ?url1=...&url2=..."), 400

    def _one(u):
        try:
            return preview_link(u)
        except Exception as e:
            return {"url": u, "error": "fetch_failed: %s" % type(e).__name__}

    with ThreadPoolExecutor(max_workers=2) as ex:
        f1, f2 = ex.submit(_one, u1), ex.submit(_one, u2)
        a, b = f1.result(timeout=_BATCH_TIMEOUT), f2.result(timeout=_BATCH_TIMEOUT)

    if "error" in a or "error" in b:
        return jsonify({"a": a, "b": b}), 502

    fields = ["title", "description", "image", "site_name", "favicon", "url"]
    diffs = []
    for f in fields:
        av, bv = a.get(f, ""), b.get(f, "")
        if av != bv:
            diffs.append({"field": f, "url1": av, "url2": bv})
    out = {
        "url1": u1,
        "url2": u2,
        "preview1": a,
        "preview2": b,
        "identical_fields": [f for f in fields if a.get(f, "") == b.get(f, "")],
        "different_fields": diffs,
        "diff_count": len(diffs),
    }
    record_billing(g.meter_key, g.plan, "diff")
    out["quota"] = quota_echo(g)
    return jsonify(out)


@app.route("/api/validate-key")
def api_validate_key():
    """Check an API key's status: plan, validity, expiry, quota. Not metered
    so users can check their key without burning quota."""
    key = (request.values.get("key") or "").strip()
    if not key:
        return jsonify(error="pass ?key=..."), 400
    info = key_status(key)
    if info is None:
        return jsonify(valid=False, error="invalid_or_expired_key"), 404
    return jsonify(valid=True, key_status=info)


@app.route("/api/status")
def api_status():
    """Self-describing service manifest: version, uptime, and the full list
    of registered API routes with their methods. Useful for SDK clients and
    for a landing-page client to render an endpoint catalogue dynamically."""
    routes = sorted(
        (
            {
                "path": r.rule,
                "methods": sorted(m for m in r.methods if m in {"GET", "POST", "PUT", "DELETE"}),
            }
            for r in app.url_map.iter_rules()
            if not r.rule.startswith("/static")
        ),
        key=lambda r: r["path"],
    )
    return jsonify(
        ok=True,
        service="LinkPeek",
        version=__version__,
        uptime_seconds=round(time.time() - _START_TIME, 1),
        endpoints=routes,
        free_daily_limit=100,
        pro_daily_limit=50000,
        docs="/api/status",
        health="/api/health",
    )


@app.route("/api/health")
def api_health():
    from decorators import PAYPAL_ME, STRIPE_LINK, PRO_PRICE_USD
    pay_method = "stripe" if STRIPE_LINK else ("paypal" if PAYPAL_ME else "manual_email")
    return jsonify(
        ok=True,
        today=daily_totals(),
        revenue={
            "pro_price_usd": PRO_PRICE_USD,
            "pay_method": pay_method,  # live when "stripe" or "paypal"
            "subscribe_url": "/api/subscribe?email=…",
        },
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
