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
    GET  /api/word-count       content stats: word count, reading time, top terms
    GET  /api/rss              detect + parse RSS/Atom feed for a URL
    GET  /api/links            extract all links, classified internal/external
    GET  /api/meta-tags        flat key→value map of every head meta tag
    GET  /api/tech-stack       detect frameworks/CMS/analytics from HTML + headers
    GET  /api/pdf-info         extract PDF metadata (version, page count, title…)
    GET  /api/sitemap-parse    parse a sitemap.xml URL into {urls:[…], sitemaps:[…]}
    GET  /api/og-image-proxy   fetch + proxy an og:image/the twitter:image bytes
    GET  /api/redirect-chain   follow a URL's HTTP redirects, report every hop
    GET  /api/content-type     GET headers only -> {content_type, charset, server, ...}
    GET  /api/ssl-info         TLS cert + protocol + cipher for an https URL
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
import http.client
import threading
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
    donate_channels,
    key_status,
    plan_catalog,
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
    http.client.HTTPException,  # InvalidURL, BadStatusLine, etc. on malformed URLs
)

# Service metadata for /api/status
__version__ = "1.8.4"
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
        # NOTE: keep this cap large enough to satisfy /api/links?limit= (max 500);
        # preview_link() callers below apply their own [:limit_clamped] slice.
        # Historically this was [:100] which silently truncated /api/links to
        # 100 results regardless of the ?limit= the caller asked for — fix #1.
        result["links"] = parser.links[:500]
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
        # make_image() lazily imports PIL; qrcode can be installed without
        # Pillow, and an ImportError at request time escapes the module-level
        # try/except, turning the endpoint into a 500. Catch ImportError AND
        # Exception (e.g. Image.DecompressionBombError, OSError on save) so the
        # caller always gets a structured 503 instead of a bare 500.
        try:
            img = qr.make_image(fill_color=_hexcol("fg", "black"),
                                back_color=_hexcol("bg", "white"))
            buf = BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
        except Exception as exc:
            return jsonify(error="qr_render_failed", detail=str(exc)[:200]), 503
        return Response(buf.getvalue(), mimetype="image/png")

    @app.route("/api/qrcode")
    @rate_limit(app)
    def api_qrcode_json():
        """QR code as base64-encoded PNG inside a JSON envelope.

        Query: ?text=... (required)  ?ecc=l|m|q|h  ?fg=RRGGBB  ?bg=RRGGBB
        Returns: {ok, text, image: "data:image/png;base64,...", ecc, size_bytes}
        Useful for clients that cannot consume raw binary image responses
        (e.g. JSON-only webhook consumers, serverless function return values).
        """
        import base64
        text = (request.values.get("text") or "").strip()
        if not text:
            return jsonify(error="pass ?text=..."), 400
        _QR_MAX_CHARS = 2000
        if len(text) > _QR_MAX_CHARS:
            return jsonify(error="text_too_long", max=_QR_MAX_CHARS, got=len(text)), 413
        _ECC = {"l": qrcode.constants.ERROR_CORRECT_L,
                "m": qrcode.constants.ERROR_CORRECT_M,
                "q": qrcode.constants.ERROR_CORRECT_Q,
                "h": qrcode.constants.ERROR_CORRECT_H}
        ecc_name = (request.values.get("ecc") or "m").strip().lower()
        ecc = _ECC.get(ecc_name, qrcode.constants.ERROR_CORRECT_M)
        qr = qrcode.QRCode(version=None, error_correction=ecc,
                           box_size=10, border=2)
        qr.add_data(text)
        qr.make(fit=True)

        def _hexcol(name, default):
            v = (request.values.get(name) or "").strip().lstrip("#")
            if not v or not re.match(r"^[0-9a-fA-F]{6}$", v):
                return default
            return "#" + v.lower()
        try:
            img = qr.make_image(fill_color=_hexcol("fg", "black"),
                                back_color=_hexcol("bg", "white"))
            buf = BytesIO()
            img.save(buf, format="PNG")
            data = buf.getvalue()
        except Exception as exc:
            return jsonify(error="qr_render_failed", detail=str(exc)[:200]), 503
        b64 = base64.b64encode(data).decode("ascii")
        out = {
            "ok": True,
            "text": text,
            "ecc": ecc_name,
            "size_bytes": len(data),
            "image": "data:image/png;base64," + b64,
        }
        out["quota"] = quota_echo(g)
        record_billing(g.meter_key, g.plan, "qrcode:%s" % text[:150])
        return jsonify(out)
except ImportError:
    @app.route("/api/qr")
    def api_qr_unavailable():
        return jsonify(error="qrcode lib not installed"), 503

    @app.route("/api/qrcode")
    def api_qrcode_json_unavailable():
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
        url = _normalize_url(url)
    except ValueError as e:
        return jsonify(error=str(e)), 400
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
        url = _normalize_url(url)
    except ValueError as e:
        return jsonify(error=str(e)), 400
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
    except ValueError as e:
        return jsonify(error=str(e)), 400
    try:
        final_url, html_text, headers = _fetch(url)
    except _FETCH_EXC as e:
        return jsonify(url=url, error="fetch_failed: %s" % type(e).__name__), 502
    head_html = _extract_head(html_text)
    parser = _PeekParser(final_url)
    try:
        parser.feed(head_html)
    except AssertionError:
        pass
    # Apply the same favicon fallback /api/preview uses so callers don't get
    # a bare "data:," empty data URI when a site uses <link rel=icon href="data:,">
    # or omits the tag entirely. Without this, the upstream raw value leaked
    # straight through and broke favicon consumers expecting a real URL.
    favicon = parser.favicon or ""
    if not favicon or favicon.startswith("data:"):
        parts = urlsplit(final_url)
        if parts.scheme and parts.netloc:
            favicon = "{}://{}/favicon.ico".format(parts.scheme, parts.netloc)
    raw_favicon = parser.favicon or ""
    out = {
        "url": final_url,
        "title": _clean(parser.title),
        "favicon": favicon,
        "raw_favicon": raw_favicon,
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


@app.route("/api/redirect-chain")
@rate_limit(app)
def api_redirect_chain():
    """Follow a URL through its HTTP redirect chain and report every hop.

    Query: ?url=https://...  (required)
    Optional: ?max_hops=10   (default 10, hard ceiling 20 — prevents infinite loops)
    Optional: ?fetch_body=0  (default 0/HEAD-only; set to 1 to GET and parse
                              the final destination as a /api/preview link card)
    Returns the ordered list of redirects:

        chain: [{step, url, status, location}, ...]
        final_url: final destination
        redirect_count: number of hops
        redirect_loop: true if the chain revisited a URL
        fetch_body=1 modes also include: {title, description, image, ...}

    Useful to debug canonical URLs, detect redirect loops, audit SEO 301
    chains, and verify affiliate/sanitizer links land where expected.
    """
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="pass ?url=https://..."), 400
    try:
        url = _normalize_url(url)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    try:
        max_hops = max(1, min(20, int(request.values.get("max_hops") or 10)))
    except ValueError:
        max_hops = 10
    want_body = (request.values.get("fetch_body") or "0").strip() in ("1", "true", "yes")

    # Manual redirect walking: opener.open follows chains by default, so we
    # use a custom HTTPRequestHandler that returns on each 3xx. Simplest
    # approach: build_opener with our own HTTPDefaultRedirectHandler that we
    # disable, then inspect r.url/status after each resp. urllib doesn't expose
    # the chain though, so we walk it one hop at a time using http.client.
    chain = []
    seen = set()
    current = url
    redirect_loop = False
    final_url = url
    final_status = 0
    hops = 0
    opener = build_opener(ProxyHandler())
    opener.addheaders = [
        ("User-Agent", "Mozilla/5.0 (compatible; LinkPeek/1.0; +https://github.com/linkpeek)"),
        ("Accept", "text/html,application/xhtml+xml,*/*;q=0.8"),
        ("Accept-Language", "en-US,en;q=0.9"),
        ("Accept-Encoding", "gzip, deflate"),
    ]
    # We override the redirect handler with a no-op so we can trace each hop.
    from urllib.request import HTTPRedirectHandler, Request as _Req
    class _NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None  # signal caller to handle the redirect manually
    trace_opener = build_opener(ProxyHandler(), _NoRedirect)
    trace_opener.addheaders = opener.addheaders
    for step in range(max_hops + 1):
        if current in seen:
            redirect_loop = True
            break
        seen.add(current)
        req = _Req(current, method="GET")
        try:
            resp = trace_opener.open(req, timeout=8.0)
            # Drain a single byte so headers are materialized.
            resp.read(1)
            final_url = resp.geturl()
            final_status = resp.getcode() or 200
            chain.append({"step": step, "url": current, "status": final_status, "location": ""})
            break  # 2xx: we've reached the final destination.
        except HTTPError as e:
            status = e.code
            loc = e.headers.get("Location") or ""
            final_url = current
            final_status = status
            if 300 <= status < 400 and loc:
                next_url = urljoin(current, loc)
                chain.append({"step": step, "url": current, "status": status, "location": next_url})
                current = next_url
                hops += 1
                continue
            # non-redirect error: stop and record what we got.
            chain.append({"step": step, "url": current, "status": status, "location": ""})
            break
        except _FETCH_EXC as e:
            return jsonify(url=url, error="fetch_failed: %s" % type(e).__name__), 502

    out = {
        "start_url": url,
        "final_url": final_url,
        "final_status": final_status,
        "redirect_count": hops,
        "redirect_loop": redirect_loop,
        "chain": chain,
    }

    if want_body and not redirect_loop and 200 <= final_status < 300 and final_url:
        try:
            preview = preview_link(final_url)
            for k in ("title", "description", "image", "site_name", "favicon"):
                if preview.get(k):
                    out[k] = preview[k]
        except _FETCH_EXC:
            pass  # body fetch is best-effort; chain is the primary payload.

    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "redirect-chain:%s" % url[:200])
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
    from decorators import TRIAL_DAYS
    return jsonify(key=key, trial_days=TRIAL_DAYS, note="use ?key=<key> on /api/preview")


@app.route("/api/subscribe")
def api_subscribe():
    """Self-serve Pro signup — issues a Pro API key and returns a payment link."""
    email = (request.values.get("email") or "").strip()
    try:
        result = subscribe(email)
    except ValueError as ve:
        return jsonify(error=str(ve)), 400
    return jsonify(result)


@app.route("/api/donate")
def api_donate():
    """Free donation/tip channels — Buy Me a Coffee, Ko-fi, GitHub Sponsors.
    Unauthenticated, read-only, $0 fixed cost.  No API keys or merchant
    account required; the operator just sets one LINKPEEK_BMC /
    LINKPEEK_KOFI / LINKPEEK_GH_SPONSORS env var to a profile URL."""
    return jsonify(donate_channels())


@app.route("/api/pricing")
def api_pricing():
    """Public plan catalogue: free vs trial vs pro pricing and limits.

    Read-only, unauthenticated, no metering — meant for landing-page
    pricing widgets and the /api/subscribe response to reference.  The
    single source of truth is decorators.plan_catalog(); the env-overridable
    limits (LINKPEEK_FREE_LIMIT/LINKPEEK_PRO_LIMIT/LINKPEEK_PRO_PRICE) flow
    through automatically so the JSON never drifts from the meter."""
    return jsonify(plan_catalog())


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
        url = _normalize_url(url)
    except ValueError as e:
        return jsonify(error=str(e)), 400
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

    import concurrent.futures as _cf
    _FutTimeout = _cf.TimeoutError
    with ThreadPoolExecutor(max_workers=2) as ex:
        f1, f2 = ex.submit(_one, u1), ex.submit(_one, u2)
        try:
            a, b = f1.result(timeout=_BATCH_TIMEOUT), f2.result(timeout=_BATCH_TIMEOUT)
        except (TimeoutError, _FutTimeout):
            # One or both previews didn't finish within the budget. We catch
            # both the builtin TimeoutError (3.11+, where futures.Timeout
            # is an alias) and the legacy 3.10 concrete subclass. Report
            # what we have so the caller gets a structured 504 not a 500.
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
    # Cap the body byte limit: floor 1 KiB, default 512 KiB, ceiling 5 MiB
    # so a caller can't turn this endpoint into a memory-exhaustion vector
    # by passing ?size=9999999999.
    _FAV_MAX = 5 * 1024 * 1024
    max_bytes = 512 * 1024
    try:
        max_bytes = max(1024, min(_FAV_MAX, int(request.values.get("size") or max_bytes)))
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
        _ = resp.read(1)  # consume 1 byte to trigger the response headers
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


# ============================================================================
# RSS / Atom feed detection + parsing (1.4.0) — stdlib xml.etree only
# ============================================================================
# Tiny RSS/Atom parser built on xml.etree.ElementTree. No dep on feedparser.
# Handles the common cases: <rss>/<channel><item> and <feed><entry> (Atom).
# Resolves relative URLs in <link> against the feed's own URL.

try:
    from xml.etree import ElementTree as _ET
    _XML_AVAILABLE = True
except ImportError:
    _XML_AVAILABLE = False


def _strip_ns(tag: str) -> str:
    """Return the local name of a possibly-namespaced tag: '{ns}foo' -> 'foo'."""
    if tag and tag[0] == "{":
        return tag.split("}", 1)[1]
    return tag


def _parse_feed(xml_text: str, base_url: str = "") -> dict:
    """Parse RSS 2.0 / Atom XML text into a dict of feed + items.

    Returns {title, link, description, type, items: [{title, link,
    description, pub_date}]} on success, or raises ValueError.
    """
    if not _XML_AVAILABLE:
        raise ValueError("xml.etree unavailable")
    if not xml_text or not xml_text.strip():
        raise ValueError("empty feed body")
    try:
        root = _ET.fromstring(xml_text)
    except _ET.ParseError as e:
        raise ValueError("xml_parse_error: %s" % str(e))

    root_local = _strip_ns(root.tag).lower()
    is_atom = root_local == "feed"
    is_rss = root_local == "rss"

    if not is_atom and not is_rss:
        raise ValueError("not a recognized feed root (<rss> or <feed>): got <%s>" % root_local)

    # Atom feeds almost always declare a default namespace
    # (xmlns="http://www.w3.org/2005/Atom"). ElementTree's bare-name lookups
    # (root.find("entry"), elem.find("title")) do NOT match elements that live
    # in a default namespace — they silently return None / []. This made every
    # real-world Atom feed (Verge, GitHub releases, most blogs) parse to an
    # empty feed: title="", item_count=0. Fix: extract the default namespace
    # URI from root.tag ("{uri}feed") and use a prefixed namespace map for the
    # Atom branch. RSS 2.0 has no default namespace and is left untouched.
    #
    # IMPORTANT: element.find("d:title") alone is a *literal tag* match — the
    # prefix is only resolved when the `namespaces` kwarg is also passed. So we
    # funnel every find/findall through _f / _fa which always pass _ns along.
    _ns = {}
    if is_atom and root.tag[0] == "{":
        _uri = root.tag[1 : root.tag.find("}")]
        _ns = {"d": _uri}  # "d" for default-namespace

    def _f(elem, tag):
        return elem.find("d:" + tag if _ns else tag, _ns if _ns else None)

    def _fa(elem, tag):
        return elem.findall("d:" + tag if _ns else tag, _ns if _ns else None)

    def _text(elem, *names):
        for n in names:
            child = _f(elem, n)
            if child is not None and child.text:
                return child.text.strip()
        return ""

    feed_info = {}
    items = []

    if is_rss:
        # RSS: root -> <channel> -> meta + <item>*
        channel = root.find("channel")
        if channel is None:
            raise ValueError("rss: missing <channel>")
        feed_info["title"] = _text(channel, "title")
        link = _text(channel, "link")
        feed_info["link"] = urljoin(base_url, link) if link else ""
        feed_info["description"] = _text(channel, "description")
        feed_info["type"] = "rss"
        for item in channel.findall("item"):
            it_link = _text(item, "link")
            it_link = urljoin(base_url, it_link) if it_link else ""
            items.append({
                "title": _text(item, "title"),
                "link": it_link,
                "description": _text(item, "description"),
                "pub_date": _text(item, "pubDate", "pubdate", "date"),
            })
    else:
        # Atom: root is <feed>, children are meta + <entry>*
        feed_info["title"] = _text(root, "title")
        # Atom <link> can have rel/type attributes; prefer rel="alternate"
        link_val = ""
        for link_elem in _fa(root, "link"):
            rel = (link_elem.get("rel") or "alternate").lower()
            href = link_elem.get("href") or ""
            if rel == "alternate" and href:
                link_val = href
                break
        if not link_val:
            link_val = _text(root, "link")
        feed_info["link"] = urljoin(base_url, link_val) if link_val else ""
        feed_info["description"] = _text(root, "subtitle", "tagline")
        feed_info["type"] = "atom"
        for entry in _fa(root, "entry"):
            it_link = ""
            for link_elem in _fa(entry, "link"):
                rel = (link_elem.get("rel") or "alternate").lower()
                href = link_elem.get("href") or ""
                if rel == "alternate" and href:
                    it_link = href
                    break
            if not it_link:
                it_link = _text(entry, "link")
            it_link = urljoin(base_url, it_link) if it_link else ""
            items.append({
                "title": _text(entry, "title"),
                "link": it_link,
                "description": _text(entry, "summary", "content"),
                "pub_date": _text(entry, "published", "updated", "published", "modified"),
            })

    feed_info["item_count"] = len(items)
    feed_info["items"] = items[:20]  # cap to keep payload bounded
    return feed_info


def _detect_and_fetch_feed(page_url: str, html_text: str, headers: dict) -> dict | None:
    """Given a page and its headers, find an RSS/Atom feed link.

    Strategy (in order):
      1. If the URL itself *is* a feed (Content-Type: application/rss+xml /
         application/atom+xml / text/xml, or body parses as <rss>/<feed>),
         return the parsed feed directly.
      2. Scan <link rel="alternate" type="application/rss+xml" ...> and
         type="application/atom+xml" in the HTML head.
      3. Try common autodiscovery paths: /feed, /rss, /atom.xml, /feed.xml,
         /rss.xml, /feed/index.xml on the same host.

    Returns the parsed feed dict, or None if no feed was found.
    """
    base_url = page_url
    ctype = (headers.get("Content-Type") or "").lower()
    looks_like_feed = (
        "rss+xml" in ctype or "atom+xml" in ctype
        or "xml" in ctype
    )
    if looks_like_feed:
        try:
            return _parse_feed(html_text, base_url=base_url)
        except ValueError:
            pass  # fall through to link discovery

    # 2. <link rel="alternate" type="application/...">
    link_re = re.compile(
        r"<link\b[^>]*\brel=[\"']?alternate[\"']?[^>]*>",
        re.IGNORECASE,
    )
    type_re = re.compile(r"\btype=[\"']?(application/(?:rss|atom)\+xml)[\"']?", re.IGNORECASE)
    href_re = re.compile(r"\bhref=[\"']([^\"']+)[\"']", re.IGNORECASE)
    head = _extract_head(html_text)
    for m in link_re.finditer(head):
        tag = m.group(0)
        if not type_re.search(tag):
            continue
        hm = href_re.search(tag)
        if hm:
            feed_url = urljoin(base_url, hm.group(1))
            try:
                furl, fhtml, fheaders = _fetch(feed_url)
                if furl is not None:
                    return _parse_feed(fhtml, base_url=furl)
            except _FETCH_EXC:
                continue
            break

    # 3. Common autodiscovery paths.
    parts = urlsplit(base_url)
    if parts.scheme and parts.netloc:
        for path in ("/feed", "/rss", "/atom.xml", "/feed.xml", "/rss.xml", "/feed/index.xml"):
            candidate = "{}://{}{}".format(parts.scheme, parts.netloc, path)
            try:
                c_url, c_html, _ = _fetch(candidate, timeout=5.0)
            except _FETCH_EXC:
                continue
            try:
                return _parse_feed(c_html, base_url=c_url)
            except ValueError:
                continue
    return None


@app.route("/api/rss")
@rate_limit(app)
def api_rss():
    """Detect and parse an RSS or Atom feed for a URL.

    Pass any page URL; the endpoint autodiscovers the feed via:
      * Content-Type sniffing (if the URL *is* a feed),
      * <link rel="alternate" type="application/rss+xml"> in the HTML head,
      * common feed paths (/feed, /rss.xml, /atom.xml, /feed.xml).

    Returns the feed title/link/description plus up to 20 items
    (each with title, link, description, pub_date). 404 if no feed found.
    """
    if not _XML_AVAILABLE:
        return jsonify(error="xml.etree unavailable on this server"), 503
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="pass ?url=https://..."), 400
    try:
        url = _normalize_url(url)
        final_url, html_text, headers = _fetch(url)
    except _FETCH_EXC as e:
        return jsonify(url=url, error="fetch_failed: %s" % type(e).__name__), 502
    try:
        feed = _detect_and_fetch_feed(final_url, html_text, headers)
    except _FETCH_EXC as e:
        return jsonify(url=url, error="feed_fetch_failed: %s" % type(e).__name__), 502
    if feed is None:
        return jsonify(url=final_url, error="no_feed_found"), 404
    feed["source_url"] = final_url
    feed["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "rss:%s" % final_url[:150])
    return jsonify(feed)


# ============================================================================
# Word count / content stats (1.4.0) — lightweight text analysis, stdlib only
# ============================================================================
# Reuses _PeekParser to strip tags, then reports word/char/read-time stats.

class _TextExtractor(HTMLParser):
    """Collect visible text from HTML, skipping <script> and <style>."""

    _SKIP = {"script", "style", "noscript", "template", "svg", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in self._SKIP and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            self.parts.append(data)

    def text(self) -> str:
        return " ".join(self.parts)


@app.route("/api/word-count")
@rate_limit(app)
def api_word_count():
    """Content statistics for a URL: word/char counts, reading time, language.

    Fetches the page (same _fetch as /api/preview), strips HTML to visible
    text, and reports:
      * word_count, char_count, char_count_no_spaces
      * reading_time_seconds (200 wpm default, configurable via ?wpm=)
      * sentence_count, avg_word_length
      * top_words (10 most frequent, stopwords excluded, configurable via ?top=)
      * title (the page <title>)

    Useful for content-quality checks, SEO tooling, and accessibility audits.
    """
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="pass ?url=https://..."), 400
    try:
        url = _normalize_url(url)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    try:
        final_url, html_text, _ = _fetch(url)
    except _FETCH_EXC as e:
        return jsonify(url=url, error="fetch_failed: %s" % type(e).__name__), 502

    # Parse <title> for context (cheap; reuses existing parser).
    head_html = _extract_head(html_text)
    title_parser = _PeekParser(final_url)
    try:
        title_parser.feed(head_html)
    except AssertionError:
        pass
    page_title = _clean(title_parser.title)

    # Strip tags to visible text.
    extractor = _TextExtractor()
    try:
        extractor.feed(html_text)
    except AssertionError:
        pass
    text = extractor.text()
    # Collapse whitespace.
    text = re.sub(r"\s+", " ", text).strip()

    # Word-level stats.
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]*", text)
    word_count = len(words)
    char_count = len(text)
    char_count_no_spaces = len(text.replace(" ", "").replace("\t", ""))

    # Sentence count: naive split on .!?  followed by space/end.
    sentence_count = max(1, len(re.findall(r"[.!?]+(?:\s|$)", text)))
    avg_word_length = (
        round(sum(len(w) for w in words) / word_count, 2) if word_count else 0
    )

    # Reading time (default 200 wpm; /api/word-count?wpm=250).
    try:
        wpm = max(50, min(1000, int(request.values.get("wpm") or 200)))
    except ValueError:
        wpm = 200
    reading_time_seconds = round((word_count / wpm) * 60) if word_count else 0

    # Top-N words by frequency (English stopwords only — keeps it stdlib).
    _STOP = {
        "the", "a", "an", "and", "or", "but", "if", "then", "else", "for",
        "of", "to", "in", "on", "at", "by", "with", "as", "is", "are", "was",
        "were", "be", "been", "being", "this", "that", "these", "those", "it",
        "its", "from", "has", "have", "had", "not", "no", "do", "does", "did",
        "will", "would", "could", "should", "can", "may", "might", "must",
        "i", "you", "he", "she", "we", "they", "them", "him", "her", "us",
        "me", "my", "your", "his", "our", "their", "so", "than", "too", "very",
    }
    try:
        top_n = max(1, min(50, int(request.values.get("top") or 10)))
    except ValueError:
        top_n = 10
    freq: dict[str, int] = {}
    for w in words:
        wl = w.lower()
        if len(wl) < 3 or wl in _STOP:
            continue
        freq[wl] = freq.get(wl, 0) + 1
    top_words = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]
    top_words_out = [{"word": w, "count": c} for w, c in top_words]

    out = {
        "url": final_url,
        "title": page_title,
        "word_count": word_count,
        "char_count": char_count,
        "char_count_no_spaces": char_count_no_spaces,
        "sentence_count": sentence_count,
        "avg_word_length": avg_word_length,
        "reading_time_seconds": reading_time_seconds,
        "reading_wpm": wpm,
        "top_words": top_words_out,
    }
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "word-count:%s" % final_url[:150])
    return jsonify(out)


# ============================================================================
# Link extraction endpoint (1.5.0) — all outbound links, classified
# ============================================================================
@app.route("/api/links")
@rate_limit(app)
def api_links():
    """Extract all links from a page, classified as internal/external.

    Query: ?url=https://...  (required)
    Optional: ?limit=50  (cap on links returned, default 200, max 500)

    Returns links with href, text, and whether they are internal (same host
    as the fetched URL) or external. Useful for SEO audits, broken-link
    checkers, and crawler seed generation.
    """
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="pass ?url=https://..."), 400
    try:
        limit_clamped = max(1, min(500, int(request.values.get("limit") or 200)))
    except ValueError:
        limit_clamped = 200
    try:
        out = preview_link(url, collect_body=True)
    except _FETCH_EXC as e:
        return jsonify(url=url, error="fetch_failed: %s" % type(e).__name__), 502

    final_url = out.get("url", url)
    base_host = (urlsplit(final_url).hostname or "").lower()
    all_links = out.get("links", [])
    internal = []
    external = []
    seen = set()
    for link in all_links[:limit_clamped]:
        href = link.get("href", "")
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        if href in seen:
            continue
        seen.add(href)
        host = (urlsplit(href).hostname or "").lower()
        entry = {"href": href, "text": link.get("text", "")}
        if not host or host == base_host:
            internal.append(entry)
        else:
            external.append(entry)
    result = {
        "url": final_url,
        "internal_links": internal,
        "external_links": external,
        "internal_count": len(internal),
        "external_count": len(external),
        "total_count": len(internal) + len(external),
    }
    result["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "links:%s" % final_url[:150])
    return jsonify(result)


# ============================================================================
# Sitemap.xml parser (1.7.1) — stdlib xml.etree, same _ET as /api/rss
# ============================================================================
# Parses a sitemap.xml URL into a flat list of <loc> URLs plus any nested
# <sitemap> entries (sitemap index files). Handles both <urlset> (a regular
# sitemap) and <sitemapindex> (a sitemap-of-sitemaps). Returns up to 500 URLs so
# a single call can't exhaust memory on a million-URL sitemap; pagination is
# left to the caller (the upstream sitemap slice is theirs to page through).
_SITEMAP_MAX_URLS = 500
_SITEMAP_MAX_FETCH = 5 * 1024 * 1024  # 5 MiB body cap — sitemaps can be large


def _parse_sitemap_xml(xml_text: str) -> dict:
    """Parse sitemap / sitemapindex XML into {type, urls, sitemaps, lastmod[]}.

    Raises ValueError on a non-sitemap root or unparseable XML.
    """
    if not _XML_AVAILABLE:
        raise ValueError("xml.etree unavailable")
    if not xml_text or not xml_text.strip():
        raise ValueError("empty sitemap body")
    try:
        root = _ET.fromstring(xml_text)
    except _ET.ParseError as e:
        raise ValueError("xml_parse_error: %s" % str(e))

    root_local = _strip_ns(root.tag).lower()
    if root_local == "urlset":
        sm_type = "urlset"
    elif root_local == "sitemapindex":
        sm_type = "sitemapindex"
    else:
        raise ValueError(
            "not a recognized sitemap root (<urlset> or <sitemapindex>): got <%s>"
            % root_local
        )

    urls = []
    sitemaps = []
    lastmods = []
    for child in root:
        child_local = _strip_ns(child.tag).lower()
        loc = ""
        lm = ""
        for sub in child:
            sub_local = _strip_ns(sub.tag).lower()
            if sub_local == "loc" and sub.text:
                loc = sub.text.strip()
            elif sub_local == "lastmod" and sub.text:
                lm = sub.text.strip()
        if not loc:
            continue
        if sm_type == "urlset":
            urls.append(loc)
            lastmods.append(lm)
        else:  # sitemapindex
            sitemaps.append({"loc": loc, "lastmod": lm})
    return {
        "type": sm_type,
        "urls": urls[:_SITEMAP_MAX_URLS],
        "url_total": len(urls),
        "url_truncated": len(urls) > _SITEMAP_MAX_URLS,
        "sitemaps": sitemaps,
        "lastmods": lastmods[:_SITEMAP_MAX_URLS],
    }


@app.route("/api/sitemap-parse")
@rate_limit(app)
def api_sitemap_parse():
    """Parse a sitemap.xml (or sitemap index) URL into structured JSON.

    Query: ?url=https://.../sitemap.xml  (required)

    Fetches the URL, parses the XML body, and returns:
      * type      — "urlset" or "sitemapindex"
      * urls      — up to 500 <loc> URLs from a <urlset>
      * sitemaps   — [{loc, lastmod}] from a <sitemapindex>
      * url_total  — actual count before the 500 cap
      * url_truncated — true if url_total > 500

    Auto-detects <urlset> vs <sitemapindex>. Uses the same stdlib xml.etree as
    /api/rss; no new deps. 415 if the URL does not serve XML, 502 on fetch
    failure, 422 on a parse error.
    """
    if not _XML_AVAILABLE:
        return jsonify(error="xml.etree unavailable on this server"), 503
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="pass ?url=https://.../sitemap.xml"), 400
    try:
        url = _normalize_url(url)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    opener = build_opener(ProxyHandler())
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; LinkPeek/1.0; +https://github.com/linkpeek)",
            "Accept": "application/xml,text/xml,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        },
        method="GET",
    )
    try:
        resp = opener.open(req, timeout=10.0)
    except HTTPError as e:
        return jsonify(url=url, error="fetch_failed: HTTP %s" % getattr(e, "code", "?")), 502
    except _FETCH_EXC as e:
        return jsonify(url=url, error="fetch_failed: %s" % type(e).__name__), 502
    ctype = (resp.headers.get("Content-Type") or "").lower()
    raw = resp.read(_SITEMAP_MAX_FETCH)
    final_url = resp.geturl()
    # Decode (reusing _decode handles gzip/deflate/charset sniffing).
    try:
        xml_text = _decode(raw, resp.headers)
    except Exception:
        xml_text = raw.decode("utf-8", errors="ignore")
    looks_xml = "xml" in ctype or xml_text.lstrip().startswith("<")
    if not looks_xml:
        return jsonify(
            url=final_url,
            content_type=ctype,
            error="not_xml (Content-Type/body not XML)",
        ), 415
    try:
        out = _parse_sitemap_xml(xml_text)
    except ValueError as e:
        return jsonify(url=final_url, error="sitemap_parse_failed: %s" % e), 422
    out["url"] = final_url
    out["content_type"] = ctype
    out["byte_size"] = len(raw)
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "sitemap:%s" % final_url[:150])
    return jsonify(out)


# ============================================================================
# OG image proxy (1.7.1) — fetch + stream the og:image bytes for a page
# ============================================================================
# Many link-card renderers (Discord, Slack, embedded CMSes) can't fetch an
# arbitrary og:image URL because of CORS / mixed-content / hotlink protection.
# This endpoint resolves the og:image (or twitter:image) for a page the same
# way /api/preview does, fetches it, and streams the bytes back with the
# upstream Content-Type — so a <img src="/api/og-image-proxy?url=…"> just works.
# ?size= caps the byte cap (default 2 MiB, ceiling 10 MiB) so a caller can't
# turn this into a memory-exhaustion vector.

@app.route("/api/og-image-proxy")
@rate_limit(app)
def api_og_image_proxy():
    """Fetch and proxy the og:image (or twitter:image) bytes for a page URL.

    Query: ?url=https://...        (the page whose image you want, not the image)
    Optional: ?size=2097152        byte cap on the proxied image (default 2 MiB,
                                   floor 1 KiB, ceiling 10 MiB)

    Returns the raw image bytes with the upstream Content-Type and a long
    Cache-Control, so a browser <img> tag works without CORS tangles. 404 if
    the page has no og:image / twitter:image; 413 if the image exceeds :size;
    502 on fetch failure.
    """
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="pass ?url=https://..."), 400
    _OG_MAX = 10 * 1024 * 1024
    max_bytes = 2 * 1024 * 1024
    try:
        max_bytes = max(1024, min(_OG_MAX, int(request.values.get("size") or max_bytes)))
    except ValueError:
        pass
    try:
        preview = preview_link(url)
    except _FETCH_EXC as e:
        return jsonify(url=url, error="fetch_failed: %s" % type(e).__name__), 502
    image_url = preview.get("image") or ""
    if not image_url or image_url.startswith("data:"):
        return jsonify(url=url, error="no_og_image_found"), 404
    opener = build_opener(ProxyHandler())
    req = Request(
        image_url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; LinkPeek/1.0; +https://github.com/linkpeek)",
            "Accept": "image/*,*/*;q=0.8",
        },
        method="GET",
    )
    try:
        resp = opener.open(req, timeout=10.0)
    except (URLError, HTTPError, socket.timeout, ConnectionError, OSError, ssl.SSLError) as e:
        return jsonify(url=image_url, error="image_fetch_failed: %s" % type(e).__name__), 502
    body = resp.read(max_bytes + 1)
    if len(body) > max_bytes:
        return jsonify(url=image_url, error="image_too_large", limit=max_bytes), 413
    ctype = (resp.headers.get("Content-Type") or "application/octet-stream").split(";")[0].strip()
    final_image_url = resp.geturl()
    record_billing(g.meter_key, g.plan, "og-image:%s" % (preview.get("url", "")[:150]))
    return Response(
        body,
        mimetype=ctype,
        headers={
            "Cache-Control": "public, max-age=3600",
            "X-LinkPeek-Source-Image": final_image_url[:500],
        },
    )


# ============================================================================
# Flat meta-tag listing (1.5.0) — every meta tag as key→value pairs
# ============================================================================
@app.route("/api/meta-tags")
@rate_limit(app)
def api_meta_tags():
    """Return every <meta> tag from the page head as a flat key→value dict.

    Query: ?url=https://...  (required)

    Unlike /api/metadata-full (which returns a nested dict and response
    headers), this endpoint gives a simple {property_or_name: content} map
    suitable for quick inspection or CMS import. Both 'property' (OG/Twitter)
    and 'name' attributes are included; if both are absent, http-equiv is
    used as the key. Duplicate keys keep the first occurrence.
    """
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="pass ?url=https://..."), 400
    try:
        url = _normalize_url(url)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    try:
        final_url, html_text, _ = _fetch(url)
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
        "meta_tags": parser.meta,
        "meta_count": len(parser.meta),
    }
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "meta-tags:%s" % final_url[:150])
    return jsonify(out)


# ============================================================================
# Tech-stack detection (1.6.0) — stdlib regex heuristics, no JS execution
# ============================================================================
# Detects front-end frameworks/libraries/CMSes from static HTML fingerprints:
# <script src="..."> paths, inline script patterns, <meta name="generator">,
# data-* attributes, and CSS class conventions. Does NOT run JavaScript, so
# client-only SPA frameworks may be missed when server rendering is absent.

_TECH_SIGNATURES = [
    # (name, list of compiled regexes — any match => detected)
    ("React", [
        re.compile(r"data-react(?:root|-hmr|-app|id)", re.I),
        re.compile(r"\breact(?:\.development|\.production)?(?:\.min)?\.js", re.I),
        re.compile(r"__REACT_DEVTOOLS_GLOBAL_HOOK__", re.I),
    ]),
    ("Next.js", [
        re.compile(r"__NEXT_DATA__", re.I),
        re.compile(r"/_next/static/", re.I),
        re.compile(r"id=\"__next\"", re.I),
    ]),
    ("Vue.js", [
        re.compile(r"\bvue(?:\.runtime)?(?:\.min)?\.js", re.I),
        re.compile(r"data-v-[0-9a-f]{8}", re.I),
        re.compile(r"<!--\s*if\s*-->", re.I),  # server-rendered Vue SSR comments
    ]),
    ("Nuxt", [
        re.compile(r"window\.__NUXT__", re.I),
        re.compile(r"data-nuxt", re.I),
        re.compile(r"/_nuxt/", re.I),
    ]),
    ("Angular", [
        re.compile(r"ng-app|ng-controller|ng-version", re.I),
        re.compile(r"@angular/", re.I),
    ]),
    ("Svelte", [
        re.compile(r"\.svelte-\w+", re.I),  # Svelte component class hashes
    ]),
    ("jQuery", [
        re.compile(r"\bjquery(?:-\d[\d.]*)?(?:\.min)?\.js", re.I),
        re.compile(r"jQuery v([\d.]+)"),
    ]),
    ("Bootstrap", [
        re.compile(r"\bbootstrap(?:\.bundle)?(?:\.min)?\.(?:js|css)", re.I),
    ]),
    ("Tailwind CSS", [
        re.compile(r"\btailwind(?:\.min)?\.css", re.I),
        re.compile(r"\bclass=\"[^\"]*\b(flex|grid|p-\d|m-\d|text-\w+-\d)\b", re.I),
    ]),
    ("Bulma", [
        re.compile(r"\bbulma(?:\.min)?\.css", re.I),
    ]),
    ("WordPress", [
        re.compile(r"<meta[^>]+name=\"generator\"[^>]+content=\"WordPress", re.I),
        re.compile(r"/wp-content/", re.I),
        re.compile(r"/wp-includes/", re.I),
    ]),
    ("Drupal", [
        re.compile(r"<meta[^>]+name=\"generator\"[^>]+content=\"Drupal", re.I),
        re.compile(r"\bdrupal\.js", re.I),
    ]),
    ("Shopify", [
        re.compile(r"cdn\.shopify\.com", re.I),
        re.compile(r"Shopify\.theme|window\.Shopify", re.I),
    ]),
    ("Squarespace", [
        re.compile(r"<meta[^>]+name=\"generator\"[^>]+content=\"Squarespace", re.I),
        re.compile(r"static1\.squarespace\.com", re.I),
    ]),
    ("Gatsby", [
        re.compile(r"___gatsby", re.I),
        re.compile(r"/gatsby-", re.I),
    ]),
    ("Hugo", [
        re.compile(r"<meta[^>]+name=\"generator\"[^>]+content=\"Hugo", re.I),
    ]),
    ("Jekyll", [
        re.compile(r"<meta[^>]+name=\"generator\"[^>]+content=\"Jekyll", re.I),
    ]),
    ("Cloudflare", [
        re.compile(r"cdn-cgi/", re.I),
        re.compile(r"__cf_bm", re.I),
    ]),
    ("Google Analytics", [
        re.compile(r"google-analytics\.com/(?:analytics|ga)\.js", re.I),
        re.compile(r"gtag/js\?id=UA-", re.I),
        re.compile(r"gtag/js\?id=G-", re.I),
    ]),
    ("Google Tag Manager", [
        re.compile(r"googletagmanager\.com/gtm\.js", re.I),
    ]),
]


def _detect_tech(html_text: str, headers: dict) -> dict:
    """Scan HTML + headers for framework/CMS fingerprints. Stdlib only."""
    # Limit scan to first 2 MiB (already capped by _fetch) and lowercased copy
    # for case-insensitive signature matching without re-lowering per regex.
    hay = html_text if len(html_text) <= _MAX_BYTES else html_text[:_MAX_BYTES]
    # Every signature regex is compiled with re.IGNORECASE, so searching the
    # lowercased haystack is sufficient and ~halves the work. The previous
    # logic did a redundant second rx.search(hay) on every non-match and a
    # dead fallback loop that could never add hits for re.I patterns.
    hay_l = hay.lower()
    detected = []
    for name, patterns in _TECH_SIGNATURES:
        hits = []
        for rx in patterns:
            if rx.search(hay_l):
                hits.append({"pattern": rx.pattern[:60]})
        if hits:
            detected.append({"name": name, "evidence": hits[:3]})
    # Generator meta tag (catch-all for CMSes we didn't pre-list).
    gen_re = re.compile(r'<meta[^>]+name="generator"[^>]+content="([^"]+)"', re.I)
    gen = gen_re.search(hay)
    generator = gen.group(1).strip() if gen else ""
    # Server header often reveals the backend stack too.
    server = (headers.get("Server") or headers.get("server") or "").strip()
    powered_by = (headers.get("X-Powered-By") or headers.get("x-powered-by") or "").strip()
    return {
        "technologies": detected,
        "detected_count": len(detected),
        "generator": generator,
        "server": server,
        "x_powered_by": powered_by,
    }


@app.route("/api/tech-stack")
@rate_limit(app)
def api_tech_stack():
    """Detect the front-end / CMS / analytics stack a page is built with.

    Query: ?url=https://...  (required)

    Scans static HTML fingerprints (script paths, data-* attributes, meta
    generator tag, CSS class conventions) plus the Server / X-Powered-By
    response headers. No JavaScript is executed, so purely client-rendered
    SPAs may report fewer frameworks than a real browser visite would.

    Returns: technologies[] (name + evidence patterns), detected_count,
    generator (raw <meta name=generator> value), server, x_powered_by.
    """
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="pass ?url=https://..."), 400
    try:
        url = _normalize_url(url)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    try:
        final_url, html_text, headers = _fetch(url)
    except _FETCH_EXC as e:
        return jsonify(url=url, error="fetch_failed: %s" % type(e).__name__), 502
    out = {"url": final_url}
    out.update(_detect_tech(html_text, headers))
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "tech-stack:%s" % final_url[:150])
    return jsonify(out)


# ============================================================================
# PDF metadata extraction (1.6.0) — pure-stdlib PDF /info + /XMP parsing
# ============================================================================
# Parses the PDF trailer /Info dict and the XMP metadata packet without
# pulling in a third-party PDF library. Handles the common producer-defined
# fields: Title, Author, Subject, Keywords, Creator, Producer, CreationDate,
# ModDate. Falls back gracefully when the trailer is malformed or absent.

_PDF_INFO_KEYS = (
    "Title", "Author", "Subject", "Keywords",
    "Creator", "Producer", "CreationDate", "ModDate",
)


def _unescape_pdf_string(raw: str) -> str:
    """Decode a PDF string literal (the bytes between ( ) or < >) to text."""
    # Hex string form: <FEFF...> or <48656C6C6F> -> decode hex then UTF-16BE.
    if raw.startswith("<"):
        hexbody = raw.strip("<>").replace("\n", "").replace("\r", "").replace(" ", "")
        try:
            b = bytes.fromhex(hexbody)
            if b.startswith(b"\xfe\xff"):
                return b[2:].decode("utf-16-be", errors="ignore")
            if b.startswith(b"\xff\xfe"):
                return b[2:].decode("utf-16-le", errors="ignore")
            return b.decode("latin-1", errors="ignore")
        except ValueError:
            return hexbody
    # Literal string form: strip outer parens, unescape balanced inner parens
    # and the common PDF escape sequences. PDF strings nest parens.
    s = raw.strip()
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1]
    out_chars = []
    i = 0
    escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f",
               "(": "(", ")": ")", "\\": "\\", "<": "<", ">": ">"}
    depth = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt in escapes:
                out_chars.append(escapes[nxt])
                i += 2
                continue
            # octal escapes \ddd (1-3 digits)
            oct_digits = ""
            j = i + 1
            while j < len(s) and j < i + 4 and s[j] in "01234567":
                oct_digits += s[j]
                j += 1
            if oct_digits:
                try:
                    out_chars.append(chr(int(oct_digits, 8) & 0xFF))
                except ValueError:
                    out_chars.append(nxt)
                i = j
                continue
            out_chars.append(nxt)
            i += 2
            continue
        if c == "(":
            depth += 1
            out_chars.append(c)
        elif c == ")":
            if depth > 0:
                depth -= 1
                out_chars.append(c)
            else:
                out_chars.append(c)
        else:
            out_chars.append(c)
        i += 1
    return "".join(out_chars)


def _parse_pdf_info(pdf_bytes: bytes) -> dict:
    """Extract /Info and basic structure metadata from a PDF byte string.

    Returns {} fields set to empty strings when absent. Adds page_count and
    pdf_version when discoverable. Raises ValueError on non-PDF input.
    """
    head = pdf_bytes[:1024]
    if b"%PDF-" not in head:
        raise ValueError("not a PDF (missing %PDF- header)")
    pdf_version = ""
    mver = re.match(rb"%PDF-(\d+\.\d+)", head)
    if mver:
        pdf_version = mver.group(1).decode("ascii", "ignore")
    info = {k: "" for k in _PDF_INFO_KEYS}
    # Page count: count /Type /Page occurrences (favour /Type\s*/Page over
    # /Pages to avoid double counting). Use a trailing boundary ([^s/]|$)
    # so a bare "/Type /Page" immediately before >> or EOF is still counted;
    # the old [^s/] char-class required a trailing byte and under-counted
    # pages whose /Page token sat at the very end of the trailer.
    pages = len(re.findall(rb"/Type\s*/Page(?:[^s/]|$)", pdf_bytes))
    # Find the trailer /Info dict: /Info <ref> near %%EOF. We scan the last
    # 64 KiB which contains the trailer for the vast majority of PDFs.
    tail = pdf_bytes[-65536:] if len(pdf_bytes) > 65536 else pdf_bytes
    # Pull every "<< ... >>" dict that contains /Title or /Producer; the last
    # one before EOF that has /Info-ish keys is usually the doc info dict.
    for key in _PDF_INFO_KEYS:
        # /Key (literal string)  OR  /Key <hex string>
        # Capture the *inner* content only (between the () or <>), so the
        # closing delimiter never leaks into the value. Non-greedy + a
        # character class that excludes the delimiters handles nested parens
        # for the common case; deeply-nested strings fall back to "".
        pat = rb"/" + key.encode("ascii") + rb"\s*(?:\((?:[^()\\]|\\.)*\)|<([0-9A-Fa-f\s]+)>)"
        for m in re.finditer(pat, tail, re.DOTALL):
            grp1 = m.group(1)
            if grp1 is None:
                # Literal (…) string: re-scan with a parens-aware capture.
                lit_pat_str = rb"/" + key.encode("ascii") + rb"\s*\(((?:[^()\\]|\\.)*)\)"
                lm = re.search(lit_pat_str, m.group(0))
                if lm:
                    info[key] = _unescape_pdf_string("(" + lm.group(1).decode("latin-1", "ignore") + ")")
                    if info[key]:
                        break
                continue
            # Hex <…> string: group(1) is the hex body (no delimiters).
            info[key] = _unescape_pdf_string("<" + grp1.decode("latin-1", "ignore") + ">")
            if info[key]:
                break
    # XMP packet: <?xpacket begin ...> ... <?xpacket end ...>. Pull a few
    # common dc: tags as a supplement when the Info dict was empty.
    xmp_re = re.compile(rb"<\?xpacket\b.*?(?:begin|begin=)[^>]*>(.*?)<\?xpacket\b.*?(?:end|end=)[^>]*\?>", re.DOTALL | re.IGNORECASE)
    xmp_match = xmp_re.search(pdf_bytes)
    xmp = {}
    if xmp_match:
        xmp_text = xmp_match.group(1).decode("utf-8", errors="ignore")
        for tag in ("dc:title", "dc:creator", "dc:description", "dc:subject", "pdf:Producer", "xmp:CreatorTool"):
            m = re.search(r"<" + re.escape(tag) + r"\b[^>]*>(.*?)</" + re.escape(tag) + r">", xmp_text, re.DOTALL)
            if m:
                # XMP values can be wrapped in <rdf:Seq><rdf:li>; just strip tags.
                val = re.sub(r"<[^>]+>", "", m.group(1)).strip()
                if val:
                    xmp[tag] = val
    out = {
        "pdf_version": pdf_version,
        "page_count": pages,
        "title": info["Title"] or xmp.get("dc:title", ""),
        "author": info["Author"] or xmp.get("dc:creator", ""),
        "subject": info["Subject"] or xmp.get("dc:description", ""),
        "keywords": info["Keywords"],
        "keywords_list": ([k.strip() for k in info["Keywords"].split(";") if k.strip()] if info["Keywords"] else []),
        "creator": info["Creator"] or xmp.get("xmp:CreatorTool", ""),
        "producer": info["Producer"] or xmp.get("pdf:Producer", ""),
        "creation_date": info["CreationDate"],
        "modification_date": info["ModDate"],
        "has_xmp": bool(xmp_match and xmp),
        "xmp": xmp if xmp else None,
    }
    return out


@app.route("/api/pdf-info")
@rate_limit(app)
def api_pdf_info():
    """Extract metadata from a PDF URL: version, page count, title/author/etc.

    Query: ?url=https://.../something.pdf  (required, must point at a PDF)

    Parses the PDF /Info dictionary and the XMP metadata packet using only the
    Python standard library — no PyPDF2 / pdfminer dependency. Returns version,
    page_count, title, author, subject, keywords, creator, producer, and the
    raw creation / modification dates (PDF date string format). 415 if the URL
    does not serve a PDF, 502 on fetch failure.
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
            "Accept": "application/pdf,application/octet-stream,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        },
        method="GET",
    )
    try:
        resp = opener.open(req, timeout=10.0)
    except HTTPError as e:
        return jsonify(url=url, error="fetch_failed: HTTP %s" % getattr(e, "code", "?")), 502
    except _FETCH_EXC as e:
        return jsonify(url=url, error="fetch_failed: %s" % type(e).__name__), 502
    ctype = (resp.headers.get("Content-Type") or "").lower()
    raw = resp.read(_MAX_BYTES)
    final_url = resp.geturl()
    # Content-Type hint, but validate by magic bytes — some hosts send
    # generic Content-Type for .pdf URLs while the body is a real PDF.
    is_pdf = "pdf" in ctype or raw[:5] == b"%PDF-"
    if not is_pdf:
        return jsonify(url=final_url, content_type=ctype,
                       error="not_a_pdf (Content-Type/bytes not a PDF)"), 415
    try:
        info = _parse_pdf_info(raw)
    except ValueError as e:
        return jsonify(url=final_url, error="pdf_parse_failed: %s" % e), 422
    out = {"url": final_url, "content_type": ctype, "byte_size": len(raw)}
    out.update(info)
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "pdf-info:%s" % final_url[:150])
    return jsonify(out)


# ============================================================================
# Structured data extraction (1.8.0) — JSON-LD + microdata, stdlib only
# ============================================================================
# Parses <script type="application/ld+json"> blocks (JSON-LD) and microdata
# itemtype/itemprop attributes from the full HTML document. Returns parsed
# JSON-LD objects (list, since a page may carry several) plus a flat list of
# microdata item scopes. No JavaScript execution; static HTML only.

_JSONLD_RE = re.compile(
    r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_ITEMSCOPE_RE = re.compile(r'<[A-Za-z][^>]*\bitemscope\b[^>]*>', re.IGNORECASE)
_ITEMTYPE_RE = re.compile(r'\bitemtype=["\']([^"\']+)["\']', re.IGNORECASE)
_ITEMPROP_RE = re.compile(r'\bitemprop=["\']([^"\']+)["\']', re.IGNORECASE)


def _extract_jsonld(html_text: str) -> list:
    """Pull every JSON-LD block and json.loads() each; skip unparseable ones."""
    out = []
    for m in _JSONLD_RE.finditer(html_text):
        raw = m.group(1).strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            continue
        # A single block may hold a top-level @graph or a bare object/list.
        if isinstance(obj, dict) and "@graph" in obj and isinstance(obj["@graph"], list):
            out.extend(obj["@graph"])
        else:
            out.append(obj)
    return out


def _extract_microdata(html_text: str) -> list:
    """Collect itemtype URLs and itemprop names from microdata attributes."""
    out = []
    for m in _ITEMSCOPE_RE.finditer(html_text):
        tag = m.group(0)
        tm = _ITEMTYPE_RE.search(tag)
        pm = _ITEMPROP_RE.findall(tag)
        out.append({
            "itemtype": tm.group(1) if tm else "",
            "itemprops": pm,
        })
    # Also capture loose itemprops outside any itemscope (rare but valid).
    loose = []
    if not _ITEMSCOPE_RE.search(html_text):
        loose = _ITEMPROP_RE.findall(html_text)
    if loose and not out:
        out.append({"itemtype": "", "itemprops": loose})
    return out


# ============================================================================
# Content-Type probe — headers-only fetch returning the parsed content type,
# charset, content length, server, and last-modified. Cheaper than /api/headers
# (one structured answer) and cheaper than /api/preview (no body parse).
# ============================================================================
def _parse_content_type(value: str) -> tuple[str, str]:
    """Split a Content-Type header into (mime, charset). charset may be ''."""
    if not value:
        return ("", "")
    parts = [p.strip() for p in value.split(";")]
    mime = parts[0].lower() if parts else ""
    charset = ""
    for p in parts[1:]:
        if p.lower().startswith("charset="):
            charset = p.split("=", 1)[1].strip().strip('"')
    return (mime, charset)


@app.route("/api/content-type")
@rate_limit(app)
def api_content_type():
    """Headers-only probe returning the parsed content type for a URL.

    Query: ?url=https://...  (required)
    Returns: url, final_url, content_type (mime), content_length, server,
    last_modified, status_code, charset. Issued as a GET but only 1 byte of
    the body is read (the response headers must materialise). 502 on fetch
    failure, 400 on a bad/missing URL (before any network call).
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
    headers: dict = {}
    final_url = url
    try:
        resp = opener.open(req, timeout=8.0)
        _ = resp.read(1)  # trigger the response headers
        final_url = resp.geturl()
        status = resp.getcode() or 200
        headers = {k: v for k, v in resp.headers.items()}
    except HTTPError as e:
        final_url = e.url or url
        status = e.code
        try:
            e.read(1)
        except (OSError, AttributeError):
            pass
        headers = {k: v for k, v in (e.headers or {}).items()}
    except _FETCH_EXC as e:
        return jsonify(url=url, error="fetch_failed: %s" % type(e).__name__), 502
    ctype_raw = headers.get("Content-Type") or headers.get("content-type") or ""
    mime, charset = _parse_content_type(ctype_raw)
    out = {
        "url": url,
        "final_url": final_url,
        "content_type": mime,
        "charset": charset,
        "content_length": headers.get("Content-Length") or headers.get("content-length") or "",
        "server": headers.get("Server") or headers.get("server") or "",
        "last_modified": headers.get("Last-Modified") or headers.get("last-modified") or "",
        "status_code": status,
    }
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "content-type:%s" % url[:150])
    return jsonify(out)


# ============================================================================
# SSL/TLS info — connect an ssl-wrapped socket, report cert + protocol + cipher.
# https URLs only (caller passes https:// or a bare host we promote to https).
# ============================================================================
def _rdn_to_dict(rdn) -> dict:
    """Flatten an ssl.py 'subject'/'issuer' field into {key: value}.

    getpeercert() returns subject/issuer as a tuple of RDNs, where each RDN
    is itself a sequence of (type, value) 2-tuples — e.g.
        ((('commonName', 'example.com'),),)
        ((('countryName', 'US'),), (('organizationName', 'Sectigo'),))
    We collapse two levels: for each RDN, for each (k, v) pair, set out[k]=v.
    Later pairs win on key collision (reasonable for the rare duplicate-O).
    A bare dict or a flat [(k,v), ...] list is also accepted for robustness.
    """
    out: dict = {}
    if not rdn:
        return out
    if isinstance(rdn, dict):
        return dict(rdn)
    try:
        for entry in rdn:
            # entry may be an RDN (sequence of (k,v) pairs) or, defensively,
            # a flat (k, v) 2-tuple if a caller passed a pre-flattened list.
            if isinstance(entry, (tuple, list)) and len(entry) == 2 and not isinstance(entry[0], (tuple, list)):
                out[str(entry[0])] = entry[1]
                continue
            for pair in entry:
                if isinstance(pair, (tuple, list)) and len(pair) == 2:
                    out[str(pair[0])] = pair[1]
    except (TypeError, ValueError):
        pass
    return out


def _ssl_probe(host: str, port: int, timeout: float = 8.0) -> dict:
    """Open an ssl-wrapped socket and extract cert + protocol + cipher.

    Two-pass: first a fully-verified context (CERT_REQUIRED) to materialise
    a parsed cert dict (valid=True); if that handshake fails (expired /
    self-signed / hostname mismatch), fall back to CERT_NONE so we can at
    least report the negotiated protocol + cipher (valid=False). If even
    the unverified handshake fails the caller surfaces it as ssl_error.
    """
    # Pass 1 — verified. getpeercert() returns a dict only when verify path
    # succeeded; with CERT_NONE it returns {} even if a cert was seen.
    out: dict = {"valid": False, "issuer": {}, "subject": {}, "not_after": ""}
    try:
        ctx = ssl.create_default_context()
        raw = socket.create_connection((host, port), timeout=timeout)
        with ctx.wrap_socket(raw, server_hostname=host) as ss:
            cert = ss.getpeercert() or {}
            if cert:
                out["valid"] = True
                out["issuer"] = _rdn_to_dict(cert.get("issuer", []))
                out["subject"] = _rdn_to_dict(cert.get("subject", []))
                out["not_after"] = cert.get("notAfter", "") or cert.get("not_after", "")
            out["protocol"] = ss.version() or ""
            c = ss.cipher()
            if c:
                out["cipher"] = {"name": c[0], "version": c[1], "bits": c[2]}
        return out
    except (ssl.SSLError, socket.error):
        pass
    # Pass 2 — unverified. We still negotiate TLS and can report protocol +
    # cipher, but the cert fields stay empty and valid stays False.
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((host, port), timeout=timeout)
        with ctx.wrap_socket(raw, server_hostname=host) as ss:
            out["protocol"] = ss.version() or ""
            c = ss.cipher()
            if c:
                out["cipher"] = {"name": c[0], "version": c[1], "bits": c[2]}
    except (ssl.SSLError, socket.error):
        raise
    return out


@app.route("/api/ssl-info")
@rate_limit(app)
def api_ssl_info():
    """Report TLS certificate + protocol + cipher for an https URL.

    Query: ?url=https://...  (required; scheme must be https)
    Returns: url, host, port, valid (bool), issuer (dict), subject (dict),
    not_after, protocol, cipher (dict)). 400 if the URL is not https or is
    malformed; 502 if the TLS handshake fails (incl. unreachable host).
    """
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="pass ?url=https://..."), 400
    try:
        url = _normalize_url(url)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        return jsonify(error="ssl-info requires an https URL"), 400
    host = parts.hostname or ""
    if not host:
        return jsonify(error="invalid host"), 400
    port = parts.port or 443
    try:
        probe = _ssl_probe(host, port)
    except _FETCH_EXC as e:
        # ssl.SSLError and socket.error/OSError are already in _FETCH_EXC,
        # so this single clause covers every handshake/connect failure.
        return jsonify(url=url, error="fetch_failed: %s" % type(e).__name__), 502
    out = {
        "url": url,
        "host": host,
        "port": port,
        "valid": probe["valid"],
        "issuer": probe["issuer"],
        "subject": probe["subject"],
        "not_after": probe["not_after"],
        "protocol": probe.get("protocol", ""),
        "cipher": probe.get("cipher", {}),
    }
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "ssl-info:%s" % host[:150])
    return jsonify(out)


@app.route("/api/structured-data")
@rate_limit(app)
def api_structured_data():
    """Extract JSON-LD and microdata structured data from a page.

    Query: ?url=https://...  (required)

    Returns:
      * json_ld  — list of parsed JSON-LD objects (each @graph flattened in)
      * microdata — list of {itemtype, itemprops[]} item scopes
      * json_ld_count, microdata_count
      * schema_types — flat list of @type values across all JSON-LD blocks
    Pure stdlib: regex extraction + json. 502 on fetch failure.
    """
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="pass ?url=https://..."), 400
    try:
        url = _normalize_url(url)
    except ValueError as e:
        # Bad scheme / SSRF guard / missing netloc -> client-side 400, not 502.
        # _FETCH_EXC includes ValueError, so without this catch the blanket
        # fetch-failed except below would mask input validation as a 502.
        return jsonify(error=str(e)), 400
    try:
        final_url, html_text, _ = _fetch(url)
    except _FETCH_EXC as e:
        return jsonify(url=url, error="fetch_failed: %s" % type(e).__name__), 502
    json_ld = _extract_jsonld(html_text)
    microdata = _extract_microdata(html_text)
    schema_types = []
    for obj in json_ld:
        if isinstance(obj, dict):
            t = obj.get("@type", "")
            if isinstance(t, list):
                schema_types.extend(str(x) for x in t)
            elif t:
                schema_types.append(str(t))
    out = {
        "url": final_url,
        "json_ld": json_ld,
        "json_ld_count": len(json_ld),
        "microdata": microdata,
        "microdata_count": len(microdata),
        "schema_types": schema_types,
    }
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "structured-data:%s" % final_url[:150])
    return jsonify(out)


@app.route("/api/status")
def api_status():
    """Self-describing service manifest (1.2.0): version, uptime, and the
    full list of registered API routes with their methods. Useful for SDK
    clients and for a landing-page client to render an endpoint catalogue
    dynamically.
    New in 1.8.4: /api/content-type (headers-only fetch -> content_type,
    charset, server, last_modified, status_code), /api/ssl-info (TLS cert
    issuer/subject/expiry + protocol + cipher for an https URL).
    35 registered endpoints total.
    New in 1.7.1: /api/sitemap-parse (parses sitemap.xml URL → URLs+sub-sitemaps),
    /api/og-image-proxy (proxies the og:image bytes for a page, with a byte cap),
    decorators.py type-annotation corruption fix (``***`` -> ``str``).
    New in 1.7.0: /api/qr 503 on missing PIL, /api/key trial_days from env,
    /api/favicons max-byte cap (DoS guard), tech-stack regex simplification +
    /api/pdf-info page count fix, /api/links cap that truncated /api/links,
    NameError on /api/diff timeout path.
    New in 1.6.0: /api/tech-stack (framework/CMS fingerprinting
    from HTML + headers), /api/pdf-info (stdlib-only PDF metadata extraction).
    New in 1.5.0: /api/links (link extraction, classified
    internal/external), /api/meta-tags (flat meta key→value map).
    New in 1.4.0: /api/rss, /api/word-count. 1.3.0: /api/oembed,
    /api/shortlink + /lp/<code>, SSRF guard in _normalize_url.
    Earlier: /api/favicons, /api/robots, /api/headers."""
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


@app.route("/api/health/json")
def api_health_json():
    """Machine-friendly health with no human-readable adapters.

    Fixed-structure Alternative to /api/health that omits currency symbols
    and subscribe_url adapters, for clients that just want numbers.
    Returns: ok, version, uptime_seconds, today: {day, count},
    now_iso (UTC, ISO 8601), free_daily_limit, pro_daily_limit.
    """
    import datetime
    routes = sorted(
        r.rule for r in app.url_map.iter_rules()
        if r.rule.startswith("/api/") and r.rule != "/api/health/json"
    )
    return jsonify(
        ok=True,
        service="LinkPeek",
        version=__version__,
        uptime_seconds=round(time.time() - _START_TIME, 1),
        today=daily_totals(),
        now_iso=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        free_daily_limit=100,
        pro_daily_limit=50000,
        endpoint_count=len(routes),
    )


@app.route("/api/stats")
def api_stats():
    """Live usage + route inventory.

    Returns: uptime_seconds, todays_request_count, free_daily_limit,
    pro_daily_limit, total_endpoints, top_endpoints (sorted route list with
    allowed methods), and service version. Useful for dashboards and
    monitoring (e.g. Uptime Kuma custom-push or Prometheus scrape via shell).
    """
    endpoints = sorted(
        (
            {
                "path": r.rule,
                "methods": sorted(m for m in r.methods if m in {"GET", "POST", "PUT", "DELETE"}),
            }
            for r in app.url_map.iter_rules()
            if r.rule.startswith("/api/")
        ),
        key=lambda e: e["path"],
    )
    today = daily_totals() or {}
    from decorators import PRO_PRICE_USD
    return jsonify(
        ok=True,
        service="LinkPeek",
        version=__version__,
        uptime_seconds=round(time.time() - _START_TIME, 1),
        todays_request_count=today.get("count", 0),
        today_day=today.get("day", ""),
        free_daily_limit=100,
        pro_daily_limit=50000,
        pro_price_usd=PRO_PRICE_USD,
        total_endpoints=len(endpoints),
        endpoints=endpoints,
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
