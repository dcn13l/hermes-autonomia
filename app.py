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
    GET  /api/favicons          proxies the favicon image bytes for a URL
    GET  /api/robots            returns a site's robots.txt parsed as JSON
    GET  /api/headers           returns only the HTTP response headers for a URL
    GET  /api/health           {ok, today:{day, count}}
    GET  /api/oembed           oEmbed 1.0 "link" provider JSON for a URL
    GET  /api/shortlink        create (?url=) or resolve (?code=) a base62 short link
    GET  /lp/<code>            302-redirect for a short code
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

# Shared "fetch blew up" exception tuple: any of these means the upstream URL
# refused/redirected-badly/timed out/cert-failed. We centralise it so every
# view returns the same 502 fetch_failed shape and we don't miss a variant
# (ConnectionResetError, builtin TimeoutError, etc.) on a copy-pasted handler.
_FETCH_EXC = (
    URLError,
    HTTPError,
    socket.timeout,
    TimeoutError,            # builtin (Python 3.10+ may surface this directly)
    ConnectionError,        # subclass of OSError: reset/refused/broken-pipe
    OSError,                # catch-all for low-level I/O surprises
    ValueError,             # _normalize_url rejections
    ssl.SSLError,
)

# Service metadata for /api/status
__version__ = "1.3.0"
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


# Schemes we will *consider* fetching. Anything else is rejected before any
# network call — this is the primary SSRF / local-file-disclosure guard.
_ALLOWED_SCHEMES = {"http", "https"}
# Conservative hostname blacklist for SSRF: loopback, link-local, RFC1918,
# RFC4193 (ULA), and IPv6 loopback / ULA / link-local. Pro users can still
# preview those if they really want to (we don't inspect the response body
# for secret leakage — this is about blocking the *fetch*), but the default
# posture for an open public API is to refuse to be an internal-network probe.
_PRIVATE_HOST_RES = (
    re.compile(r"^127\."),                       # 127.0.0.0/8
    re.compile(r"^10\."),                        # 10.0.0.0/8
    re.compile(r"^192\.168\."),                  # 192.168.0.0/16
    re.compile(r"^172\.(1[6-9]|2[0-9]|3[01])\."),  # 172.16.0.0/12
    re.compile(r"^169\.254\."),                  # 169.254.0.0/16 (link-local)
    re.compile(r"^(0|255|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7]))\."),  # 0/8,255/8,endpoints
    re.compile(r"^\[?$"),                         # bare-bracket edge
)
# IPv6: ::1 loopback, fc00::/7 ULA, fe80::/10 link-local. We only block
# the well-known textual forms because full IPv6 parsing is heavy stdlib.
_PRIVATE_V6 = ("::1", "fc", "fd", "fe80", "fe90", "fea0", "feb0", "fec0", "fed0", "fee0", "fef0")


def _is_private_host(netloc: str) -> bool:
    # Strip userinfo and port: "user:pass@host:port" -> "host"
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    # Strip bracketed IPv6 [::1]:port -> ::1
    if netloc.startswith("["):
        end = netloc.find("]")
        host = netloc[1:end] if end != -1 else netloc
        return any(host.startswith(p) for p in _PRIVATE_V6)
    # Strip port (v4 or hostname)
    host = netloc.rsplit(":", 1)[0] if netloc.count(":") == 1 else netloc
    host = host.lower()
    if host in ("localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"):
        return True
    for rx in _PRIVATE_HOST_RES:
        if rx.match(host):
            return True
    return False


def _normalize_url(url: str, allow_private: bool = False) -> str:
    if not url:
        raise ValueError("missing url")
    url = url.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", url):
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ValueError("unsupported scheme: %s (only http/https)" % parts.scheme)
    if not parts.netloc:
        raise ValueError("invalid url")
    if not allow_private and _is_private_host(parts.netloc):
        raise ValueError("refusing to fetch non-public host")
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
        # QR capacity is ~2,953 bytes at the lowest error correction; cap at
        # 2,000 to keep error-correction healthy and block oversized payloads
        # from turning the endpoint into a memory/CPU chew toy.
        _QR_MAX_CHARS = 2000
        if len(text) > _QR_MAX_CHARS:
            return jsonify(error="text_too_long", max=_QR_MAX_CHARS, got=len(text)), 413
        # Optional ECC level via ?ecc=L|M|Q|H (default M).
        _ECC = {"l": qrcode.constants.ERROR_CORRECT_L,
                "m": qrcode.constants.ERROR_CORRECT_M,
                "q": qrcode.constants.ERROR_CORRECT_Q,
                "h": qrcode.constants.ERROR_CORRECT_H}
        ecc = _ECC.get((request.values.get("ecc") or "m").strip().lower(),
                       qrcode.constants.ERROR_CORRECT_M)
        qr = qrcode.QRCode(version=None, error_correction=ecc,
                           box_size=10, border=2)
        qr.add_data(text)
        qr.make(fit=True)
        # Allow caller to customise colours via ?fg= & ?bg= (hex, no #).
        def _hexcol(name, default):
            v = (request.values.get(name) or "").strip().lstrip("#")
            if not v:
                return default
            if not re.match(r"^[0-9a-fA-F]{6}$", v):
                return default
            return "#" + v.lower()
        img = qr.make_image(fill_color=_hexcol("fg", "black"),
                            back_color=_hexcol("bg", "white"))
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
    except _FETCH_EXC as e:
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
    except _FETCH_EXC as e:
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
    except _FETCH_EXC as e:
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
    except _FETCH_EXC as e:
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
        try:
            a, b = f1.result(timeout=_BATCH_TIMEOUT), f2.result(timeout=_BATCH_TIMEOUT)
        except TimeoutError:
            # One or both previews didn't finish within the budget; report what
            # we have so the caller gets a structured 504 instead of a 500.
            def _res(fut, u):
                if fut.done() and fut.exception() is None:
                    try:
                        return fut.result(timeout=0)
                    except Exception:
                        pass
                return {"url": u, "error": "timeout: diff"}
            return jsonify({"url1": u1, "url2": u2,
                            "a": _res(f1, u1), "b": _res(f2, u2)}), 504

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


# ============================================================================
# Proxy-style utility endpoints (1.2.0): favicon image, robots.txt, headers
# ============================================================================
@app.route("/api/favicons")
@rate_limit(app)
def api_favicons():
    """Fetch and proxy a site's favicon image bytes directly.

    Resolves the favicon the same way /api/preview does (link rel=icon, then
    /favicon.ico fallback) and streams the image back with the upstream
    Content-Type, so a browser <img src="/api/favicons?url=…"> just works
    without CORS tangles. ?size= controls body byte cap (default 512 KiB).
    """
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="pass ?url=https://..."), 400
    max_bytes = 512 * 1024
    try:
        max_bytes = max(1024, int(request.values.get("size") or max_bytes))
    except ValueError:
        pass
    try:
        preview = preview_link(url)
    except _FETCH_EXC as e:
        return jsonify(url=url, error="fetch_failed: %s" % type(e).__name__), 502
    favicon_url = preview.get("favicon") or ""
    if not favicon_url or favicon_url.startswith("data:"):
        return jsonify(url=url, error="no_favicon_found"), 404
    opener = build_opener(ProxyHandler())
    req = Request(
        favicon_url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; LinkPeek/1.0; +https://github.com/linkpeek)",
            "Accept": "image/*,*/*;q=0.8",
        },
        method="GET",
    )
    try:
        resp = opener.open(req, timeout=8.0)
    except (URLError, HTTPError, socket.timeout, ConnectionError, OSError, ssl.SSLError) as e:
        return jsonify(url=favicon_url, error="favicon_fetch_failed: %s" % type(e).__name__), 502
    body = resp.read(max_bytes + 1)
    if len(body) > max_bytes:
        return jsonify(url=favicon_url, error="favicon_too_large"), 413
    ctype = (resp.headers.get("Content-Type") or "application/octet-stream").split(";")[0].strip()
    record_billing(g.meter_key, g.plan, "favicon:%s" % (preview.get("url", "")[:150]))
    return Response(body, mimetype=ctype, headers={"Cache-Control": "public, max-age=3600"})


@app.route("/api/robots")
@rate_limit(app)
def api_robots():
    """Fetch a site's /robots.txt and return it parsed as JSON.

    Returns: status (200/404/etc), raw (the raw text), user_agents (a list of
    {user_agent, allow:[], disallow:[], crawl_delay}) and sitemaps ([urls]).
    Resolves the URL to its scheme://host/robots.txt, following redirects.
    """
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="pass ?url=https://..."), 400
    try:
        url = _normalize_url(url)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    parts = urlsplit(url)
    robots_url = "{}://{}/robots.txt".format(parts.scheme, parts.netloc)
    opener = build_opener(ProxyHandler())
    req = Request(
        robots_url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; LinkPeek/1.0; +https://github.com/linkpeek)",
            "Accept": "text/plain,*/*;q=0.8",
        },
        method="GET",
    )
    status = 200
    raw = ""
    try:
        resp = opener.open(req, timeout=8.0)
        raw = resp.read(_MAX_BYTES).decode("utf-8", errors="ignore")
        status = resp.getcode() or 200
    except HTTPError as e:
        status = e.code
        if status == 404:
            raw = ""  # missing robots.txt means "allow everything"
        else:
            try:
                raw = e.read(_MAX_BYTES).decode("utf-8", errors="ignore")
            except (OSError, AttributeError):
                pass
    except (URLError, socket.timeout, ConnectionError, OSError, ssl.SSLError) as e:
        return jsonify(url=robots_url, error="fetch_failed: %s" % type(e).__name__), 502

    # Parse robots.txt into structured JSON (RFC 9309, forgiving).
    user_agents = []
    sitemaps = []
    current = None
    for line in raw.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()
        if field == "user-agent":
            if not current or current.get("user_agent") != value:
                current = {"user_agent": value, "allow": [], "disallow": [], "crawl_delay": None}
                user_agents.append(current)
        elif field == "allow" and current is not None:
            current["allow"].append(value)
        elif field == "disallow" and current is not None:
            if value == "":
                continue  # "Disallow:" with empty value means "allow all"
            current["disallow"].append(value)
        elif field == "crawl-delay" and current is not None:
            try:
                current["crawl_delay"] = float(value)
            except ValueError:
                current["crawl_delay"] = None
        elif field == "sitemap":
            sitemaps.append(value)

    out = {
        "url": robots_url,
        "status": status,
        "raw": raw,
        "user_agents": user_agents,
        "sitemaps": sitemaps,
    }
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "robots:%s" % parts.netloc[:150])
    return jsonify(out)


@app.route("/api/headers")
@rate_limit(app)
def api_headers():
    """Return only the HTTP response headers for a URL — no body parsing.

    Issues a GET but discards the body (reads at most 1 byte just to trigger
    the response). Returns final_url (after redirects), status, and headers
    as a flat dict. Cheap and fast for link-card builders that only need
    Content-Type / charset / caching / canonical hints.
    """
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="pass ?url=https://..."), 400
    try:
        url = _normalize_url(url)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    opener = build_opener(ProxyHandler())
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; LinkPeek/1.0; +https://github.com/linkpeek)",
            "Accept": "*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        },
        method="GET",
    )
    status = 200
    try:
        resp = opener.open(req, timeout=8.0)
        _ = resp.read(1)  # consume precisely nothing of the body
        final_url = resp.geturl()
        status = resp.getcode() or 200
        headers = {k: v for k, v in resp.headers.items()}
    except HTTPError as e:
        final_url = e.url or url
        status = e.code
        try:
            e.read(1)  # drain the tiny error body
        except (OSError, AttributeError):
            pass
        headers = {k: v for k, v in (e.headers or {}).items()}
    except (URLError, socket.timeout, ConnectionError, OSError, ssl.SSLError) as e:
        return jsonify(url=url, error="fetch_failed: %s" % type(e).__name__), 502
    out = {
        "url": final_url,
        "status": status,
        "headers": headers,
    }
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "headers:%s" % url[:150])
    return jsonify(out)


# ============================================================================
# oEmbed provider endpoint (oembed.com spec) — link-card-friendly JSON
# ============================================================================
@app.route("/api/oembed")
@rate_limit(app)
def api_oembed():
    """Minimal oEmbed provider response per oembed.com spec 1.0.

    Query: ?url=https://...  (required)
    Returns a "link" type oEmbed JSON document with title, author_name
    (derived from og:site_name or hostname), provider_name, thumbnail_url
    (the og:image / twitter:image), and the original url. Consumers like
    Discord/Slack/iA Writer that speak oEmbed can use this directly.
    """
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="pass ?url=https://..."), 400
    try:
        preview = preview_link(url)
    except _FETCH_EXC as e:
        return jsonify(url=url, error="fetch_failed: %s" % type(e).__name__), 502
    parts = urlsplit(preview.get("url") or url)
    provider = preview.get("site_name") or parts.hostname or ""
    author = preview.get("site_name") or parts.hostname or ""
    # oEmbed defines width/height as *positive integers*; we don't actually
    # fetch the image to dimension it, so omit them (the spec permits omitting
    # dimensions for "link" type — only "photo"/"video" require them).
    out = {
        "type": "link",
        "version": "1.0",
        "title": preview.get("title") or "",
        "author_name": author,
        "author_url": "%s://%s" % (parts.scheme, parts.netloc) if parts.scheme and parts.netloc else "",
        "provider_name": provider,
        "provider_url": "%s://%s" % (parts.scheme, parts.netloc) if parts.scheme and parts.netloc else "",
        "cache_age": 3600,
        "url": preview.get("url") or url,
    }
    if preview.get("image"):
        out["thumbnail_url"] = preview["image"]
    if preview.get("description"):
        out["description"] = preview["description"]
    record_billing(g.meter_key, g.plan, "oembed:%s" % url[:150])
    out["quota"] = quota_echo(g)
    return jsonify(out)


# ============================================================================
# Short-link endpoint — reversible base62 short codes, no DB required.
# ============================================================================
# In-process short-link store: {code: {url: ..., created: ts, hits: N}}.
# Survives for the process lifetime only — appropriate for a v1 micro-service.
# An operator wanting persistence swaps _SHORTLINKS for a DB-backed dict-like.
_SHORTLINKS: dict[str, dict] = {}
_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _base62_encode(n: int) -> str:
    if n == 0:
        return _BASE62[0]
    out = []
    while n:
        out.append(_BASE62[n % 62])
        n //= 62
    return "".join(reversed(out))


@app.route("/api/shortlink")
@rate_limit(app)
def api_shortlink():
    """Create or resolve a short link.

    Two modes:
      * Create:  ?url=https://...  ->  {code, short_url, original_url}
      * Resolve: ?code=XXXX          ->  {code, original_url, hits}

    Codes are base62 of an incrementing counter, prefixed 'lp/' so a single
    LinkPeek host can serve them. Idempotent: the same input URL always
    returns the same code (we scan the small in-memory map).
    """
    want_url = (request.values.get("url") or "").strip()
    want_code = (request.values.get("code") or "").strip().upper()
    if not want_url and not want_code:
        return jsonify(error="pass ?url=... to create, or ?code=... to resolve"), 400

    # Resolve mode.
    if want_code:
        rec = _SHORTLINKS.get(want_code)
        if not rec:
            return jsonify(error="not_found", code=want_code), 404
        rec["hits"] = rec.get("hits", 0) + 1
        record_billing(g.meter_key, g.plan, "shortlink:resolve")
        return jsonify({
            "code": want_code,
            "original_url": rec["url"],
            "hits": rec["hits"],
            "created": rec["created"],
            "quota": quota_echo(g),
        })

    # Create mode — validate the URL (scheme + non-private host + netloc).
    try:
        normalized = _normalize_url(want_url)
    except ValueError as e:
        return jsonify(error=str(e)), 400

    # Idempotent: return existing code if we already shortened this URL.
    for code, rec in _SHORTLINKS.items():
        if rec.get("url") == normalized:
            record_billing(g.meter_key, g.plan, "shortlink:reissue")
            return jsonify({
                "code": code,
                "short_url": "/lp/%s" % code,
                "original_url": normalized,
                "already_existed": True,
                "quota": quota_echo(g),
            })

    code = _base62_encode(len(_SHORTLINKS) + 1)
    _SHORTLINKS[code] = {"url": normalized, "created": int(time.time()), "hits": 0}
    record_billing(g.meter_key, g.plan, "shortlink:create")
    return jsonify({
        "code": code,
        "short_url": "/lp/%s" % code,
        "original_url": normalized,
        "quota": quota_echo(g),
    })


@app.route("/lp/<code>")
def lp_redirect(code):
    """Resolve a short code and 302-redirect to the original URL."""
    rec = _SHORTLINKS.get(code.upper())
    if not rec:
        return jsonify(error="not_found", code=code), 404
    rec["hits"] = rec.get("hits", 0) + 1
    from flask import redirect
    return redirect(rec["url"], code=302)


@app.route("/api/status")
def api_status():
    """Self-describing service manifest (1.2.0): version, uptime, and the
    full list of registered API routes with their methods. Useful for SDK
    clients and for a landing-page client to render an endpoint catalogue
    dynamically. New in 1.3.0: /api/oembed (oEmbed link provider),
    /api/shortlink + /lp/<code> (base62 short links), SSRF guard in
    _normalize_url. Earlier: /api/favicons (image proxy), /api/robots
    (robots.txt as JSON), /api/headers (headers-only fetch)."""
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
