#!/usr/bin/env python3
"""
LinkPeek — link preview API + QR code generator.

Single Flask app. Endpoints:
    GET  /                     homepage (serves ./index.html)
    GET  /api/preview          metered link-preview extraction
    GET  /api/extract          raw meta + links + headings (deeper crawl)
    GET  /api/metadata         combined preview + response headers (one call)
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
    GET  /api/dns-lookup       resolve A/AAAA/CNAME/MX/TXT/NS records via DoH
    GET  /api/readability      extract main article text (Readability heuristics)
    GET  /api/og-image         generate a placeholder 1200x630 OG image (PNG)
    GET  /api/json-validate    validate JSON syntax (?json= or ?url=src)
    GET  /api/social-embed     ready-to-paste social link-card bundle (OG+Twitter)
    GET  /api/ssl-check        TLS cert expiry/issuer summary + days-until-expiry
    GET  /api/security-txt    fetch + parse RFC 9116 /.well-known/security.txt
    GET  /api/wayback        Internet Archive Wayback Machine snapshot lookup
    GET  /api/perf-timing    server-side TTFB + download time + total bytes for a URL
    GET  /api/slugify        slugify a text string into a URL-safe slug
    GET  /api/password-strength  analyze password complexity + suggestions
    GET  /api/cron-parser    parse 5-field cron expr → description + next runs
    GET  /api/qr-with-logo   QR code with embedded centred logo (PNG or base64)
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
    _load_keys,
    _save_keys,
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
__version__ = "1.15.0"
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
        # NOTE: the original form ``except (ImportError, Exception)`` was a
        # buggy catch-all — Exception already subsumes everything, so ImportError
        # was dead weight and *any* error (incl. AttributeError from a non-brotli
        # object) was silently swallowed. Split the two intents: ImportError
        # means the brotli module is absent (leave data compressed, _decode will
        # fall back to utf-8 ignore), and OSError covers decompress failures
        # (corrupt/truncated brotli body) without masking unrelated bugs.
        try:
            import brotli  # type: ignore
        except ImportError:
            pass  # not installed — leave data untouched; decode ignores it
        else:
            try:
                data = brotli.decompress(data)
            except OSError:
                pass  # corrupt brotli; fall back to the raw bytes
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
    # Strip userinfo: "user:pass@host:port" -> "host:port"
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    # Strip bracketed IPv6 [::1]:port -> ::1
    if netloc.startswith("["):
        end = netloc.find("]")
        host = netloc[1:end] if end != -1 else netloc
        host = host.lower()
        # Check IPv6 loopback/ULA/link-local prefixes
        if host == "::1" or host.startswith("::1"):
            return True
        return any(host.startswith(p) for p in _PRIVATE_V6)
    # Strip port (v4 or hostname) — but be careful with bare IPv6 (no brackets).
    # ::1 has colons but is a host, not host:port. Use a regex-free parse.
    if ":" in netloc:
        parts = netloc.split(":")
        # More than one colon → likely bare IPv6 (e.g., "::1", "fe80::1")
        if len(parts) > 2:
            host = netloc.lower()
            # Check IPv6 private prefixes
            if host == "::1" or host.startswith("::1"):
                return True
            return any(host.startswith(p) for p in _PRIVATE_V6)
        else:
            host = parts[0].lower()
    else:
        host = netloc.lower()
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
        record_billing(g.meter_key, g.plan, "qr:%s" % text[:150])
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


# ============================================================================
# /api/metadata — combined preview + response headers in one round trip.
# Saves clients two calls (preview + headers) when they need both the parsed
# link-card fields AND the raw HTTP response headers (Content-Type, Server,
# Last-Modified, status, ...). Reuses preview_link + a headers-only probe.
# ============================================================================
@app.route("/api/metadata")
@rate_limit(app)
def api_metadata():
    """Combined link-preview + response-headers view in a single JSON payload.

    Query: ?url=https://...  (required)

    Returns the four keys a link-card builder plus a cache/SEO auditor need
    in one round trip:
      * preview — the same dict /api/preview returns (title, description,
        image, site_name, favicon, url)
      * headers — final_url, status, headers{} (same shape as /api/headers)
      * content_type — parsed (mime, charset) for convenience
      * quota

    One network round trip fetches HTML for the preview; the headers come
    from the same fetch (no second request), so this is no slower than
    /api/preview alone. 400 on a missing/bad URL, 502 on fetch failure.
    """
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="pass ?url=https://..."), 400
    try:
        url = _normalize_url(url)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    try:
        # preview_link re-fetches via _fetch internally; we also need the
        # raw headers, so do one _fetch here and reuse it for both. This
        # keeps the endpoint to a single outbound request.
        final_url, html_text, headers = _fetch(url)
    except _FETCH_EXC as e:
        return jsonify(url=url, error="fetch_failed: %s" % type(e).__name__), 502
    # Build the preview from the already-fetched HTML (no second fetch).
    head_html = _extract_head(html_text)
    parser = _PeekParser(final_url)
    try:
        parser.feed(head_html)
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
    status = 200
    # _fetch swallowed HTTPError into the body when available; pull status
    # via a headers-only probe so the caller sees the upstream status code.
    try:
        h_opener = build_opener(ProxyHandler())
        h_req = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; LinkPeek/1.0; +https://github.com/linkpeek)",
                "Accept": "*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate",
            },
            method="GET",
        )
        h_resp = h_opener.open(h_req, timeout=8.0)
        h_resp.read(1)
        status = h_resp.getcode() or 200
    except HTTPError as he:
        status = he.code
        try:
            he.read(1)
        except (OSError, AttributeError):
            pass
    except _FETCH_EXC:
        pass  # status stays 200; preview is the primary payload
    ctype_raw = (
        headers.get("Content-Type") or headers.get("content-type") or ""
    )
    mime, charset = _parse_content_type(ctype_raw)
    out = {
        "url": final_url,
        "preview": {
            "url": final_url,
            "title": title,
            "description": description,
            "image": image,
            "site_name": site_name,
            "favicon": favicon,
        },
        "headers": {
            "final_url": final_url,
            "status": status,
            "headers": {k: v for k, v in headers.items()},
        },
        "content_type": {"mime": mime, "charset": charset},
    }
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "metadata:%s" % url[:200])
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
# /api/security-headers — fetch response headers and grade the security ones.
# A dead-simple retention feature: many sites (and the developers building on
# LinkPeek) want a one-call "is my site's HTTP security posture any good?"
# that /api/headers alone can't answer (it returns raw headers without
# analysis). This endpoint fetches once (same ProxyHandler/no-proxy path as
# /api/headers), checks for the well-known security response headers, and
# returns a present/missing verdict plus a 0-100 score. stdlib-only.
# ============================================================================
# Headers we inspect, ordered roughly by protective impact. Each entry maps to
# a short human description surfaced in the response so a non-expert can act on
# the verdict without consulting a separate doc.
_SEC_HEADERS = (
    ("strict-transport-security",
     "HSTS", "Forces HTTPS and prevents SSL-stripping downgrade attacks."),
    ("content-security-policy",
     "CSP", "Mitigates XSS / data-injection by restricting resource sources."),
    ("x-frame-options",
     "X-Frame-Options", "Clickjacking guard (deny/sameorigin framing)."),
    ("x-content-type-options",
     "X-Content-Type-Options", "Stops MIME-sniffing (nosniff)."),
    ("referrer-policy",
     "Referrer-Policy", "Controls how much Referer leaks to third parties."),
    ("permissions-policy",
     "Permissions-Policy", "Locks down browser features (camera, geo, mics)."),
    ("cross-origin-opener-policy",
     "COOP", "Isolates browsing context — Spectre / cross-origin attack guard."),
    ("cross-origin-embedder-policy",
     "COEP", "Required for cross-origin isolation; blocks no-corb loads."),
    ("cross-origin-resource-policy",
     "CORP", "Restricts who can embed this resource cross-origin."),
)
# Weights sum to 1.0; HSTS + CSP carry the most weight since their absence is
# the most consequential. Score = round(sum of met weights * 100).
_SEC_WEIGHTS = {
    "strict-transport-security": 0.20,
    "content-security-policy": 0.20,
    "x-frame-options": 0.15,
    "x-content-type-options": 0.10,
    "referrer-policy": 0.10,
    "permissions-policy": 0.10,
    "cross-origin-opener-policy": 0.05,
    "cross-origin-embedder-policy": 0.05,
    "cross-origin-resource-policy": 0.05,
}


@app.route("/api/security-headers")
@rate_limit(app)
def api_security_headers():
    """Audit a URL's HTTP security response headers and score them 0-100.

    Query: ?url=https://...  (required)

    Does ONE fetch (a one-byte GET so the response headers materialize, like
    /api/headers) and checks for the well-known security headers: HSTS, CSP,
    X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy,
    COOP/COEP/CORP. Returns each header's presence + raw value, a 0-100
    weighted score, a grade (A-F), and a short advisory per missing header so
    a non-expert developer can act immediately. 400 on a bad/missing URL,
    502 on fetch failure.
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
    final_url = url
    status = 200
    headers = {}
    https = urlsplit(url).scheme.lower() == "https"
    try:
        resp = opener.open(req, timeout=8.0)
        _ = resp.read(1)  # trigger the response headers (no body needed)
        final_url = resp.geturl()
        status = resp.getcode() or 200
        headers = {k.lower(): v for k, v in resp.headers.items()}
    except HTTPError as e:
        final_url = e.url or url
        status = e.code
        headers = {k.lower(): v for k, v in (e.headers or {}).items()}
        try:
            e.read(1)
        except (OSError, AttributeError):
            pass
    except _FETCH_EXC as e:
        return jsonify(url=url, error="fetch_failed: %s" % type(e).__name__), 502

    checks = []
    score = 0.0
    for hdr, short, desc in _SEC_HEADERS:
        value = headers.get(hdr)
        present = value is not None and value != ""
        if present:
            score += _SEC_WEIGHTS.get(hdr, 0.0)
        # HSTS only counts on https:// — an http:// URL can never send it,
        # so we don't penalise absence there (avoids a misleading low score
        # for a site that simply wasn't probed over TLS).
        if hdr == "strict-transport-security" and not https:
            note = "N/A over http:// (HSTS only applies to HTTPS responses)."
        elif not present:
            note = "MISSING — " + desc
        else:
            note = "present"
        checks.append({
            "header": hdr,
            "label": short,
            "present": present,
            "value": value or "",
            "advice": note,
        })

    # HSTS is only meaningful on HTTPS; drop its weight from the denominator
    # when scoring an http:// URL so the score isn't artificially deflated.
    max_score = 1.0 if https else (1.0 - _SEC_WEIGHTS["strict-transport-security"])
    score_pct = round((score / max_score) * 100) if max_score else 0
    if score_pct >= 90:
        grade = "A"
    elif score_pct >= 75:
        grade = "B"
    elif score_pct >= 60:
        grade = "C"
    elif score_pct >= 40:
        grade = "D"
    else:
        grade = "F"

    out = {
        "url": final_url,
        "status": status,
        "scheme": "https" if https else "http",
        "score": score_pct,
        "grade": grade,
        "checks": checks,
        "missing_count": sum(1 for c in checks if not c["present"]
                             and not c["advice"].startswith("N/A")),
        "summary": ("HTTPS security posture audit based on response headers. "
                    "See /api/headers for the raw header dump."),
    }
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "security-headers:%s" % url[:150])
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


# ============================================================================
# DNS lookup (1.8.5) — resolve A/AAAA/CNAME/MX/TXT/NS for a domain
# ============================================================================
# Uses DNS-over-HTTPS (DoH) via Google's public resolver
# (https://dns.google/resolve?name=…&type=…) so a single uniform stdlib call
# path covers EVERY record type — including MX/TXT/CNAME/NS that the stdlib
# socket module cannot expose. DoH is plain HTTPS to a well-known endpoint,
# needs no DNS library, and works behind any NAT/firewall that allows 443.
# It is the most portable stdlib-only DNS approach and keeps the product free
# of any third-party DNS dependency.

# DNS type numbers as returned by the DoH JSON API (RFC 8484 wire format
# types, exposed name->code for the pretty-printed response).
_DNS_TYPE_CODE = {1: "A", 28: "AAAA", 5: "CNAME", 15: "MX", 16: "TXT", 2: "NS"}
_DNS_TYPE_NAME = {v: k for k, v in _DNS_TYPE_CODE.items()}
# Record types the endpoint accepts via ?type=; default set covers the
# common developer use cases. We always normalise to upper-case.
_DNS_ALLOWED_TYPES = frozenset(_DNS_TYPE_NAME)
_DNS_DEFAULT_TYPES = ("A", "AAAA", "MX", "TXT", "NS")


def _doh_resolve(name: str, rtype: str, timeout: float = 6.0) -> dict:
    """One DoH query against dns.google for a single record type.

    Returns {type, records: [str], status, ttl} where ``status`` is the
    DNS RCODE (0=NOERROR, 3=NXDOMAIN, etc.) and ``records`` is the list
    of bare string data values (MX kept as "priority host", TXT with
    surrounding quotes stripped). Raises ValueError on a non-NOERROR
    transport problem; caller decides how to surface RCODE 3.
    """
    # dns.google JSON API: GET /resolve?name=…&type=NAME
    url = "https://dns.google/resolve?name=%s&type=%s" % (
        urlquote(name, safe=""),
        rtype,
    )
    opener = build_opener(ProxyHandler())
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; LinkPeek/1.0; +https://github.com/linkpeek)",
            "Accept": "application/dns-json",
        },
        method="GET",
    )
    try:
        resp = opener.open(req, timeout=timeout)
        raw = resp.read(64 * 1024)  # DoH JSON responses are small; cap defensively
        payload = json.loads(raw.decode("utf-8", errors="ignore"))
    except (URLError, HTTPError, socket.timeout, ConnectionError, OSError,
            ssl.SSLError, ValueError) as exc:
        raise ValueError("doh_failed: %s" % type(exc).__name__)

    status = int(payload.get("Status", -1))
    answers = payload.get("Answer") or []
    expected_code = _DNS_TYPE_NAME.get(rtype, 0)
    records = []
    ttl = 0
    for a in answers:
        # The DoH API echoes the queried type in `Answer[].type`; CNAME
        # queries sometimes ALSO include the canonical A record, so filter
        # to only the requested type unless the caller asked for CNAME (in
        # which case the CNAME target is what they want, not the chain).
        atype = a.get("type")
        if rtype != "CNAME" and atype != expected_code:
            continue
        data = a.get("data", "")
        if rtype == "TXT" and isinstance(data, str):
            # DoH returns TXT values quoted: "v=spf1 -all". Strip ONE pair.
            if len(data) >= 2 and data[0] == '"' and data[-1] == '"':
                data = data[1:-1]
        records.append(data)
        if a.get("TTL", 0) > ttl:
            ttl = a.get("TTL")
    return {"type": rtype, "status": status, "ttl": ttl, "records": records}


@app.route("/api/dns-lookup")
@rate_limit(app)
def api_dns_lookup():
    """Resolve DNS records (A/AAAA/CNAME/MX/TXT/NS) for a domain.

    Query:
      ?domain=example.com          required; a bare hostname or a URL
                                   (we strip scheme + path + port first)
      ?type=A,AAAA,MX,TXT,NS       optional comma list; default A,AAAA,MX,TXT,NS
      ?type=MX                     single type also accepted

    Uses DNS-over-HTTPS against Google's public resolver
    (https://dns.google/resolve) so every record type — including
    MX/TXT/CNAME/NS that the stdlib ``socket`` module cannot expose — works
    through one uniform stdlib HTTPS call. No third-party DNS library.

    Returns: domain, types_queried, results: {TYPE: {status, ttl, records[]}},
    resolved_via ("doh/google"), and a flat summary {has_A, has_AAAA, has_MX,
    has_TXT, has_NS}. 400 on a bad domain, 502 if DoH is unreachable.
    """
    raw = (request.values.get("domain") or request.values.get("url") or "").strip()
    if not raw:
        return jsonify(error="pass ?domain=example.com"), 400
    # Accept either a bare hostname or a full URL; reduce to the registrable
    # host. ``urlsplit`` handles "example.com", "https://example.com/x",
    # "http://user:pass@example.com:8080/p?q=1" uniformly.
    if "://" in raw:
        parts = urlsplit(raw)
        domain = parts.hostname or ""
    else:
        # urlsplit gives netloc='example.com:80' for "example.com:80"; we
        # pull the host port-free. Strip any trailing path/query too.
        candidate = raw.split("/", 1)[0]
        # Strip brackets from an [ipv6]:port form just in case.
        if candidate.startswith("["):
            end = candidate.find("]")
            domain = candidate[1:end] if end != -1 else candidate
        else:
            domain = candidate.rsplit(":", 1)[0] if candidate.count(":") == 1 else candidate
    domain = domain.lower().strip(".")
    if not domain or not re.match(r"^[a-z0-9.\-]+$", domain):
        return jsonify(error="invalid_domain", domain=raw), 400
    if len(domain) > 253:
        return jsonify(error="domain_too_long", max=253, got=len(domain)), 400

    # Parse the requested types; default to the documented set.
    type_param = (request.values.get("type") or "").strip().upper()
    if type_param:
        wanted = [t.strip() for t in type_param.split(",") if t.strip()]
        wanted = [t for t in wanted if t in _DNS_ALLOWED_TYPES]
        if not wanted:
            return jsonify(
                error="invalid_type",
                allowed=sorted(_DNS_ALLOWED_TYPES),
            ), 400
    else:
        wanted = list(_DNS_DEFAULT_TYPES)

    results = {}
    for rtype in wanted:
        try:
            results[rtype] = _doh_resolve(domain, rtype)
        except ValueError as exc:
            results[rtype] = {"type": rtype, "error": str(exc)}
    # If every type errored on what looks like a transport failure (none got a
    # status field), surface a 502 so the caller knows DoH itself was down.
    any_ok = any("status" in v for v in results.values())
    out = {
        "domain": domain,
        "types_queried": wanted,
        "results": results,
        "resolved_via": "doh/google",
        "summary": {
            "has_A": bool(results.get("A", {}).get("records")),
            "has_AAAA": bool(results.get("AAAA", {}).get("records")),
            "has_MX": bool(results.get("MX", {}).get("records")),
            "has_TXT": bool(results.get("TXT", {}).get("records")),
            "has_NS": bool(results.get("NS", {}).get("records")),
        },
    }
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "dns-lookup:%s" % domain[:150])
    # 502 only when literally nothing resolved (DoH unreachable),
    # not when individual types legitimately returned NXDOMAIN (status 3).
    if not any_ok:
        out["error"] = "doh_unreachable"
        return jsonify(out), 502
    return jsonify(out)


# ============================================================================
# Readability extraction (1.8.5) — pull the main article text from a page
# ============================================================================
# Builds on _TextExtractor (visible-text stripper from /api/word-count) but
# adds real article-detection heuristics:
#   1. Prefer semantic main containers: <article>, <main>, [role=main],
#      #main, #content, #post, .post-content, .entry-content, .article-body.
#   2. Otherwise score block-level <div>/<section> elements by link density
#      (the classic Readability signal: high-link-density blocks are
#      boilerplate nav/comments, low-link-density text blocks are content).
#   3. Extract headings (h1-h3) inside the chosen container as a structured
#      outline, and the first paragraph as a plain-text excerpt.

_READ_SEMANTIC_RE = re.compile(
    r"<(article|main)\b[^>]*>(.*?)</\1>",
    re.IGNORECASE | re.DOTALL,
)
_READ_ROLE_MAIN_RE = re.compile(
    r"<(\w+)[^>]*\brole=[\"']main[\"'][^>]*>(.*?)</\1>",
    re.IGNORECASE | re.DOTALL,
)
_READ_ID_CLASS_RE = re.compile(
    r"<(div|section|article)\b[^>]*\b(?:id|class)=[\"'][^\"']*"
    r"(?:main|content|post|article|entry|story|article-body)[^\"']*[\"'][^>]*>(.*?)</\1>",
    re.IGNORECASE | re.DOTALL,
)
# Link density helper: ratio of chars inside <a>…</a> to total text length.
_READ_A_RE = re.compile(r"<a\b[^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_READ_BLOCK_RE = re.compile(
    r"<(div|section|article|main)\b[^>]*>(.*?)</\1>",
    re.IGNORECASE | re.DOTALL,
)
_READ_P_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
_READ_TAG_RE = re.compile(r"<[^>]+>")
_READ_WHITESPACE_RE = re.compile(r"\s+")


def _readability_strip(html_fragment: str) -> tuple[str, int]:
    """Strip tags from an HTML fragment and return (text, text_char_len)."""
    text = _READ_TAG_RE.sub(" ", html_fragment)
    text = _READ_WHITESPACE_RE.sub(" ", text).strip()
    return (text, len(text))


def _readability_link_density(html_fragment: str) -> float:
    """Ratio of characters inside <a>…</a> to total visible-text characters.

    A block whose text is mostly anchor text (link density near 1.0) is
    almost certainly navigation/comment boilerplate, not article body.
    """
    stripped, total = _readability_strip(html_fragment)
    if total == 0:
        return 1.0  # empty block -> treat as all-links so it scores 0
    link_chars = 0
    for m in _READ_A_RE.finditer(html_fragment):
        lt, _ = _readability_strip(m.group(1))
        link_chars += len(lt)
    return link_chars / total if total else 1.0


def _extract_readability(html_text: str, base_url: str) -> dict:
    """Pull the main article text out of an HTML document, heuristically.

    Returns {title, excerpt, text, word_count, char_count, headings[],
    container_used, method}. ``method`` is "semantic" (matched <article>/
    <main>/role=main) or "scoring" (block-level density heuristic) so the
    caller can tell how confident the extraction was.
    """
    # Title from head (cheap; reuses the existing parser).
    head_html = _extract_head(html_text)
    tp = _PeekParser(base_url)
    try:
        tp.feed(head_html)
    except AssertionError:
        pass
    title = _clean(tp.title)

    # ---- Step 1: try semantic main containers (most reliable) -------------
    candidate_html = ""
    method = "none"
    # <article> / <main> are the strongest semantic signals; prefer them
    # over role/id/class which can be mis-applied. Try <article> first since
    # it is more specific, then <main>, then role=main.
    for rx, label in (
        (_READ_SEMANTIC_RE, "article-or-main"),
        (_READ_ROLE_MAIN_RE, "role-main"),
        (_READ_ID_CLASS_RE, "id-class"),
    ):
        matches = rx.findall(html_text)
        if matches:
            # Pick the longest match (the main article is usually the
            # largest semantic container on the page).
            matches.sort(key=lambda mv: len(mv[1]), reverse=True)
            candidate_html = matches[0][1]
            method = "semantic:" + label
            break

    # ---- Step 2: link-density block scoring (Readability-style) ----------
    # If no semantic container matched, score every top-level <div>/<section>
    # block. Content score = text_len * (1 - link_density). High-link-density
    # blocks (nav, related-links, comments) score low even if they are long.
    if not candidate_html:
        best_score = -1.0
        best_html = ""
        for m in _READ_BLOCK_RE.finditer(html_text):
            inner = m.group(2)
            if len(inner) < 200:
                continue  # too short to plausibly be the article body
            text, tlen = _readability_strip(inner)
            if tlen < 200:
                continue
            ld = _readability_link_density(inner)
            # content score: long low-link-density blocks win.
            score = tlen * (1.0 - ld)
            if score > best_score:
                best_score = score
                best_html = inner
        if best_html:
            candidate_html = best_html
            method = "scoring"

    # ---- Step 3: extract text, headings, and an excerpt ------------------
    if not candidate_html:
        # Last resort: fall back to the whole <body> if we have one; this
        # is the least-confident path so mark it explicitly.
        body_re = re.compile(r"<body\b[^>]*>(.*?)</body>", re.IGNORECASE | re.DOTALL)
        bm = body_re.search(html_text)
        candidate_html = bm.group(1) if bm else html_text
        method = "fallback:body"

    full_text, char_count = _readability_strip(candidate_html)
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]*", full_text)

    # Headings inside the chosen container -> structured outline (h1-h3).
    # We only collect headings within the candidate HTML: if the candidate
    # IS an <article>/<main> its headings are the article's own; if the
    # candidate was scored from a <div> we still get the section's outline.
    headings = []
    h_re = re.compile(r"<(h[1-3])\b[^>]*>(.*?)</\1>", re.IGNORECASE | re.DOTALL)
    for hm in h_re.finditer(candidate_html):
        htext, _ = _readability_strip(hm.group(2))
        htext = _READ_WHITESPACE_RE.sub(" ", htext).strip()
        if htext:
            headings.append({"level": int(hm.group(1)[1]), "text": htext[:300]})

    # Excerpt: first non-empty <p> inside the candidate.
    excerpt = ""
    for pm in _READ_P_RE.finditer(candidate_html):
        ptext, plen = _readability_strip(pm.group(1))
        ptext = _READ_WHITESPACE_RE.sub(" ", ptext).strip()
        if plen >= 40:
            excerpt = ptext[:500]
            break
    if not excerpt and full_text:
        excerpt = full_text[:500]

    return {
        "title": title,
        "excerpt": excerpt,
        "text": full_text[:20000],  # cap the returned body to keep payloads sane
        "text_truncated": len(full_text) > 20000,
        "full_text_length": len(full_text),
        "word_count": len(words),
        "char_count": char_count,
        "headings": headings[:50],
        "heading_count": len(headings),
        "container_used": method,
    }


@app.route("/api/readability")
@rate_limit(app)
def api_readability():
    """Extract the main article text from a page (Readability-style).

    Query: ?url=https://...  (required)
    Optional: ?max_chars=20000   cap on returned ``text`` (default 20000,
                                  floor 500, ceiling 100000)

    Uses stdlib HTMLParser heuristics — no readability/jsdom dep:
      1. Prefer semantic containers (<article>, <main>, role=main,
         id/class matching main|content|post|article|entry|story).
      2. Otherwise score block-level <div>/<section> by link density:
         longest low-link-density block wins (Readability's core signal).
      3. Fall back to <body> if neither matched.

    Returns: title, excerpt (first paragraph), text (article body, capped),
    full_text_length, word_count, char_count, headings[] (outline inside
    the chosen container), container_used (which heuristic matched).
    502 on fetch failure, 400 on a bad/missing URL.
    """
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="pass ?url=https://..."), 400
    try:
        url = _normalize_url(url)
    except ValueError as e:
        # Mirror /api/structured-data's careful split so input-validation
        # errors land as 400, not masked as 502 fetch_failed.
        return jsonify(error=str(e)), 400
    try:
        max_cap = max(500, min(100000, int(request.values.get("max_chars") or 20000)))
    except ValueError:
        max_cap = 20000
    try:
        final_url, html_text, _ = _fetch(url)
    except _FETCH_EXC as e:
        return jsonify(url=url, error="fetch_failed: %s" % type(e).__name__), 502
    out = _extract_readability(html_text, final_url)
    # Re-apply the per-call max_chars cap (the helper uses a 20000 default;
    # an explicit cap is honoured here so ?max_chars=100000 works).
    if len(out["text"]) > max_cap:
        out["text"] = out["text"][:max_cap]
        out["text_truncated"] = True
    out["url"] = final_url
    out["max_chars"] = max_cap
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "readability:%s" % final_url[:150])
    return jsonify(out)


# ============================================================================
# OG image generator (1.8.5) — placeholder OpenGraph image from text
# ============================================================================
# Many developers need a placeholder og:image for pages without one, or for
# local/staging/CI builds. This endpoint renders a 1200x630 PNG (the
# canonical OG image size) from a ?title= (and optional ?subtitle=) using
# Pillow (PIL), which is already an optional dep of /api/qr. When PIL is not
# installed the endpoint returns a structured 503, exactly like /api/qr.

try:
    from PIL import Image, ImageDraw, ImageFont

    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False


def _hex_to_rgb(h: str, default: tuple) -> tuple:
    """Convert a '#RRGGBB' or 'RRGGBB' string to an (r,g,b) tuple."""
    v = (h or "").strip().lstrip("#")
    if not re.match(r"^[0-9a-fA-F]{6}$", v):
        return default
    return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))


def _load_og_font(size: int):
    """Best-effort system font; fall back to PIL's default bitmap font.

    We try a curated list of common font paths so the rendering works on
    most Linux distros without a font install step. If none found, PIL's
    load_default() is used — it only has one size, so the requested ``size``
    is ignored in that case (the text still renders, just smaller).

    Returns a PIL ImageFont (FreeTypeFont when a TTF is found, else the
    built-in bitmap font) — both expose the ``textlength``/``getsize``
    metrics used by _wrap_text.
    """
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/local/share/fonts/DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    # Final fallback: PIL's built-in bitmap font (size is fixed/small).
    return ImageFont.load_default()


def _wrap_text(text: str, font, draw, max_width: int) -> list:
    """Greedy word-wrap ``text`` to fit ``max_width`` pixels for ``font``.

    Returns a list of lines (str). Tries to break on spaces; one over-long
    word is allowed to overflow rather than being mid-word-split, which
    keeps titles readable.
    """
    if not text:
        return []
    words = text.split()
    lines = []
    cur = ""
    for w in words:
        trial = (cur + " " + w).strip()
        try:
            # PIL >= 9.2: textlength(). Older: textlength() may be absent;
            # fall back to font.getsize() (deprecated in 10.0 but works).
            width = draw.textlength(trial, font=font)
        except (AttributeError, TypeError):
            try:
                width = font.getsize(trial)[0]  # type: ignore[attr-defined]
            except (AttributeError, TypeError):
                width = len(trial) * (size_guess := 10)
        if width <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _render_og_image(
    title: str,
    subtitle: str = "",
    bg: tuple = (23, 23, 32),
    fg: tuple = (245, 245, 250),
    accent: tuple = (99, 102, 241),
) -> bytes:
    """Render a 1200x630 PNG og:image with title + subtitle text.

    Layout: a dark background, a left accent bar, the title word-wrapped and
    vertically centered, the subtitle below it in a smaller faded weight.
    Returns the PNG bytes.
    """
    W, H = 1200, 630
    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)
    # Left accent bar (~20px wide, full height) — a pop of brand colour.
    draw.rectangle([0, 0, 20, H], fill=accent)
    # Optional top-right corner square (subtle visual interest).
    draw.rectangle([W - 90, 0, W, 90], fill=accent)

    # Title font: a large bold size that scales with title length so a
    # 1-word and a 12-word title both fit inside the safe area.
    title_size = 96 if len(title) <= 20 else (72 if len(title) <= 60 else 56)
    title_font = _load_og_font(title_size)
    subtitle_font = _load_og_font(max(28, title_size // 3))

    # Safe text area (left padding accounts for the accent bar).
    pad_x = 80
    safe_w = W - pad_x - 80
    title_lines = _wrap_text(title, title_font, draw, safe_w)
    if not title_lines:
        title_lines = [title or "Untitled"]

    # Compute the stacked height so the block is vertically centered.
    line_h = int(title_size * 1.18)
    block_h = len(title_lines) * line_h
    if subtitle:
        sub_h = int(max(28, title_size // 3) * 1.4)
        block_h += sub_h + 32
    y = max(40, (H - block_h) // 2)

    for line in title_lines:
        draw.text((pad_x, y), line, font=title_font, fill=fg)
        y += line_h

    if subtitle:
        sub_lines = _wrap_text(subtitle, subtitle_font, draw, safe_w)
        for sline in sub_lines[:3]:  # cap subtitle to 3 lines
            draw.text((pad_x, y), sline, font=subtitle_font, fill=(170, 170, 190))
            y += int(max(28, title_size // 3) * 1.25)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


@app.route("/api/og-image")
@rate_limit(app)
def api_og_image():
    """Generate a placeholder OpenGraph image (1200x630 PNG) from text.

    Query:
      ?title=Your Title            required (1-200 chars); the headline text
      ?subtitle=Optional subtitle  optional; rendered smaller, below title
      ?bg=1a1a2e                   optional; hex background colour (no #)
      ?fg=f5f5fa                   optional; hex foreground/title colour
      ?accent=6366f1              optional; hex accent-bar colour

    Renders a 1200x630 PNG (the canonical OG image size) with the title
    word-wrapped and vertically centred, a left brand accent bar, and the
    optional subtitle beneath the title in a faded weight. Uses Pillow
    (PIL) — the same optional dep as /api/qr. Returns image/png bytes
    (so a <meta property=og:image> or <img src> can point at it directly).
    503 if PIL is not installed, 400 if ?title= is missing/empty.
    """
    if not _PIL_AVAILABLE:
        return jsonify(error="pillow lib not installed"), 503
    title = (request.values.get("title") or "").strip()
    if not title:
        return jsonify(error="pass ?title=Your+Title"), 400
    if len(title) > 200:
        return jsonify(error="title_too_long", max=200, got=len(title)), 413
    subtitle = (request.values.get("subtitle") or "").strip()
    if len(subtitle) > 300:
        return jsonify(error="subtitle_too_long", max=300, got=len(subtitle)), 413
    bg = _hex_to_rgb(request.values.get("bg") or "", (23, 23, 32))
    fg = _hex_to_rgb(request.values.get("fg") or "", (245, 245, 250))
    accent = _hex_to_rgb(request.values.get("accent") or "", (99, 102, 241))
    try:
        png_bytes = _render_og_image(title, subtitle, bg=bg, fg=fg, accent=accent)
    except Exception as exc:
        # Catch any PIL render/save error so the endpoint returns a clean
        # 503 (matching /api/qr's posture) instead of a bare 500.
        return jsonify(error="og_image_render_failed", detail=str(exc)[:200]), 503
    record_billing(g.meter_key, g.plan, "og-image:%s" % title[:150])
    return Response(
        png_bytes,
        mimetype="image/png",
        headers={
            "Cache-Control": "public, max-age=86400",
            "X-LinkPeek-Generated": "true",
        },
    )


@app.route("/api/status")
def api_status():
    """Self-describing service manifest (1.2.0): version, uptime, and the
    full list of registered API routes with their methods. Useful for SDK
    clients and for a landing-page client to render an endpoint catalogue
    dynamically.
    New in 1.8.5: /api/dns-lookup (DoH-based A/AAAA/CNAME/MX/TXT/NS resolver),
    /api/readability (extract main article text via Readability-style
    link-density heuristics, stdlib only), /api/og-image (generate a
    placeholder 1200x630 OG image PNG from ?title=+?subtitle= via Pillow).
    New in 1.8.4: /api/content-type (headers-only fetch -> content_type,
    charset, server, last_modified, status_code), /api/ssl-info (TLS cert
    issuer/subject/expiry + protocol + cipher for an https URL).
    Registered endpoints total: see the ``endpoints`` array length in this
    response (the count is computed dynamically from the Flask url_map so it
    never drifts from the actual route table).
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


# ──────────────────────────────────────────────────────────────────────────
# PayPal IPN / webhook endpoint — auto-activates paid Pro accounts.
#
# Receives a PayPal webhook/Instant Payment Notification event, extracts the
# payer email + gross amount, finds matching keys in keys.json, and flips
# paid=true + plan="pro" when payment_status == COMPLETED and amount >= $5.00.
#
# Accepts both JSON (PayPal REST webhooks) and form-encoded (classic IPN).
# Does NOT verify PayPal's HMAC/IPN signature (we have no PayPal Partner
# credentials or NowPayments API key set yet) — once a key is set, a future
# wake can verify the payload. Cheap, defensive: the endpoint is harmless to
# random SPAM (no key matches → 404, no state change).
# ──────────────────────────────────────────────────────────────────────────
@app.route("/api/webhook/paypal", methods=["POST"])
def api_webhook_paypal():
    import time as _time
    import os as _os
    import hmac as _hmac
    import hashlib as _hashlib
    from decorators import PRO_PRICE_USD as PRO_PRICE

    # ── Security: verify the request actually came from PayPal ──
    # PayPal REST webhooks send specific headers. If none are present,
    # deny the request to prevent fraudulent key activations.
    _pp_headers = [
        "PayPal-Transmission-Id",
        "Paypal-Transmission-Id",
        "PAYPAL-TRANSMISSION-ID",
    ]
    has_pp_header = any(h in request.headers for h in _pp_headers)
    # Optional shared-secret token: if LINKPEEK_WEBHOOK_SECRET is set,
    # the caller must pass it as the X-Webhook-Secret header.
    _configured_secret = _os.environ.get("LINKPEEK_WEBHOOK_SECRET", "")
    _caller_secret = request.headers.get("X-Webhook-Secret", "")
    if not has_pp_header:
        if _configured_secret:
            # Secret-token mode: allow if the secret matches.
            if not _hmac.compare_digest(_caller_secret, _configured_secret):
                return jsonify(ok=False, error="unauthorized: invalid_webhook_secret"), 401
        else:
            # No PayPal headers AND no secret configured → deny.
            # This prevents anyone from POSTing fake webhooks.
            return jsonify(ok=False, error="unauthorized: missing_paypal_headers"), 401

    # ── Parse payload ────────────────────────────────────────────
    payload = None
    if request.is_json:
        payload = request.get_json(silent=True) or {}
    elif request.method == "POST":
        payload = request.form.to_dict() or {}
    else:
        payload = {}
    if not payload:
        return jsonify(ok=False, error="empty_or_unsupported_payload"), 400

    # PayPal REST webhooks put fields under .resource; classic IPN is flat.
    resource = payload.get("resource") if isinstance(payload, dict) else None
    if isinstance(resource, str):
        try:
            resource = json.loads(resource)
        except Exception:
            resource = {}
    if not isinstance(resource, dict):
        resource = {}

    # Extract email from various possible shapes.
    payer_email = (
        (resource.get("payer") or {}).get("email_address")
        or (resource.get("payer_info") or {}).get("email_address")
        or (payload.get("payer") or {}).get("email_address")
        or (payload.get("payer_info") or {}).get("email_address")
        or payload.get("payer_email")
        or payload.get("email")
        or (resource.get("payer") or {}).get("email")
        or (payload.get("payer") or {}).get("email")
    )
    if not payer_email or not isinstance(payer_email, str):
        return jsonify(ok=False, error="no_payer_email"), 400
    payer_email = payer_email.strip().lower()
    if "@" not in payer_email:
        return jsonify(ok=False, error="invalid_payer_email"), 400

    # Extract payment status + amount.
    payment_status = (
        payload.get("payment_status")
        or resource.get("status")
        or payload.get("status")
        or ""
    )
    if isinstance(payment_status, str):
        payment_status = payment_status.upper()
    else:
        payment_status = ""

    amount_str = (
        payload.get("mc_gross")
        or payload.get("payment_gross")
        or resource.get("amount", {}).get("value")
        or payload.get("amount", {}).get("value")
        or "0"
    )
    try:
        amount = float(amount_str)
    except (TypeError, ValueError):
        amount = 0.0

    # ── Load keys + update paid status ───────────────────────────
    keys = _load_keys()
    activated = []
    no_change = []
    for ap_key, meta in keys.items():
        if not isinstance(meta, dict):
            continue
        key_email = (meta.get("email") or "").strip().lower()
        if key_email != payer_email:
            continue
        # Already paid → mark no_change and move on.
        if meta.get("plan") == "pro" and meta.get("paid"):
            no_change.append({"key": ap_key, "reason": "already_pro_paid"})
            continue
        # Non-completed payment or below threshold → mark but don't upgrade.
        if payment_status != "COMPLETED":
            no_change.append({"key": ap_key,
                              "reason": "payment_not_completed_" + payment_status})
            continue
        if amount < (PRO_PRICE or 5.0):
            no_change.append({"key": ap_key,
                              "reason": "amount_below_threshold_" + str(amount)})
            continue
        # Upgrade: flip paid + pro, stamp audit trail.
        previous_plan = meta.get("plan", "free")
        meta["plan"] = "pro"
        meta["paid"] = True
        meta["auto_activated"] = _time.time()
        meta["activation_source"] = "paypal_webhook"
        meta["activation_amount_usd"] = amount
        # meta is already keys[ap_key] (iter returns the live dict), so no
        # reassignment needed; persist the whole keys file below.
        activated.append({"key": ap_key, "amount_usd": amount,
                          "previous_plan": previous_plan})

    if activated:
        _save_keys(keys)
        record_billing("paypal", "pro_activated", "paypal_webhook:" + payer_email[:150])

    # 200 OK either way so PayPal stops retrying (we handle via state).
    try:
        return jsonify(
            ok=True,
            payer_email=payer_email,
            amount_usd=amount,
            status=payment_status,
            keys_activated=[a["key"] for a in activated],
            keys_no_change=no_change,
        ), 200 if (activated or no_change) else 404
    except Exception:
        return jsonify(
            ok=True, payer_email=payer_email,
            keys_activated=[a["key"] for a in activated],
        )


# ──────────────────────────────────────────────────────────────────────────
# /api/broken-links — fetch a page, extract all URLs from it, HEAD-request
# each one, report which return 4xx/5xx or timeout. SEO maintenance use case:
# blog/CMS maintainers check pages for broken links. Reuses existing plumbing
# (_fetch, _normalize_url, urljoin). Cap at 20 links with 4s HEAD timeout each.
# ──────────────────────────────────────────────────────────────────────────
@app.route("/api/broken-links")
@rate_limit(app)
def api_broken_links():
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="missing url parameter"), 400
    try:
        url = _normalize_url(url)
    except ValueError as e:
        return jsonify(error=str(e)), 400

    try:
        limit = max(1, min(20, int(request.values.get("limit") or 10)))
    except (ValueError, TypeError):
        limit = 10
    try:
        head_timeout = max(1, min(8, int(request.values.get("timeout") or 4)))
    except (ValueError, TypeError):
        head_timeout = 4

    # ── Fetch source page via existing _fetch ──────────────────────
    try:
        final_url, html_text, headers = _fetch(url, timeout=10)
    except _FETCH_EXC as e:
        return jsonify(error="fetch_failed", detail=str(e)), 502

    content_type = (headers.get("Content-Type") or "").lower()
    if not (content_type.startswith("text/html") or content_type.startswith("application/xhtml")):
        return jsonify(error="source is not HTML (got %s)" % content_type), 422

    # Extract all href="..." links — regex (stdlib only, no BeautifulSoup).
    href_re = re.compile(r"""(?:href|src)\s*=\s*["']([^"']+)["']""", re.I)
    raw_links = href_re.findall(html_text)[:limit * 2]

    # Normalize relative + dedupe + cap.
    seen = set()
    links = []
    for l in raw_links:
        full = urljoin(final_url, l)
        if full.startswith(("http://", "https://")) and full not in seen:
            seen.add(full)
            links.append(full)
            if len(links) >= limit:
                break

    if not links:
        return jsonify(broken_links=[], broken_count=0, checked_count=0,
                       quota=quota_echo(g))

    # ── Parallel HEAD checks (with GET fallback) ────────────────
    # Some servers/CDNs reject HEAD with 405 or 501 even though the resource
    # exists and a GET would return 200. Treating those as "broken" silently
    # produces false positives — the headline failure mode of this endpoint.
    # Fix: on 405/501 (and 502 HEAD-only quirks on some app servers), retry
    # with a GET that reads exactly 1 byte so the link is graded on its real
    # status, not the server's dislike of HEAD.
    _UA = "Mozilla/5.0 (compatible; LinkPeek/1.0; +https://github.com/linkpeek)"

    def _check(lk):
        def _probe(method):
            req = Request(lk, method=method, headers={"User-Agent": _UA})
            r = opener.open(req, timeout=head_timeout)
            code = r.getcode() or 200
            r.read(1)  # drain a byte so headers/body are materialized
            r.close()
            return code

        opener = build_opener(ProxyHandler())
        try:
            status = _probe("HEAD")
            if status in (405, 501):
                # HEAD not allowed — retry GET and grade on the real status.
                status = _probe("GET")
            return {"url": lk, "status": status, "broken": status >= 400}
        except HTTPError as e:
            status = e.code
            if status in (405, 501):
                # Server rejected HEAD; fall back to GET to avoid a false
                # positive "broken" verdict on a resource that works fine.
                try:
                    status = _probe("GET")
                    return {"url": lk, "status": status,
                            "broken": status >= 400, "method": "GET"}
                except HTTPError as ge:
                    return {"url": lk, "status": ge.code,
                            "broken": ge.code >= 400, "error": ge.reason}
            return {"url": lk, "status": status,
                    "broken": status >= 400, "error": e.reason}
        except _FETCH_EXC as e:
            return {"url": lk, "status": None, "broken": True,
                    "error": str(e)[:100]}

    results = []
    broken = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        for r in pool.map(_check, links):
            results.append({
                "url": r["url"], "status": r.get("status"),
                "broken": r.get("broken", False),
                "error": r.get("error"),
            })
            if r.get("broken"):
                broken.append(r["url"])

    # Source netloc for billing label (parts is local to _normalize_url).
    try:
        src_netloc = urlsplit(url).netloc[:150]
    except Exception:
        src_netloc = ""
    record_billing(g.meter_key, g.plan, "broken-links:%s" % src_netloc)
    return jsonify(
        source=url,
        checked_count=len(results),
        broken_count=len(broken),
        broken_links=broken,
        results=results,
        quota=quota_echo(g),
    )


# ============================================================================
# /api/email-validate — RFC 5322 syntax check + MX record lookup.
# Provides deliverability info without sending mail. Reuses the stdlib DoH
# resolver (_doh_resolve) so no third-party DNS library is required.
# ============================================================================
# Pragmatic RFC 5322 subset (no obsolete folding, no quoted local parts with
# escapes — covers virtually every real-world address).  Rejects IPs, quoted
# pairs, and the "comment" forms; we deliberately never green-light those.
_EMAIL_RE = re.compile(
    r"^[A-Z0-9](?:[A-Z0-9._+-]*[A-Z0-9])?"
    r"@(?:[A-Z0-9](?:[A-Z0-9-]*[A-Z0-9])?\.)+[A-Z]{2,}$",
    re.IGNORECASE,
)


@app.route("/api/email-validate")
@rate_limit(app)
def api_email_validate():
    """Validate one email address for syntax (RFC 5322 subset) + MX records.

    Query: ?email=user@example.com  (required)
    Optional: ?timeout=6            DoH query timeout in seconds (1..15)

    Returns: email, valid_syntax (bool), mx_records: [...], has_mx (bool),
    domain, plus the DoH status/ttl for the MX query. 400 on a missing email,
    502 only if the DoH resolver itself is unreachable (the address is still
    reported with valid_syntax; we just cannot claim deliverability).
    """
    email = (request.values.get("email") or "").strip()
    if not email:
        return jsonify(error="pass ?email=user@example.com"), 400
    try:
        timeout = max(1, min(15, int(request.values.get("timeout") or 6)))
    except (ValueError, TypeError):
        timeout = 6

    valid_syntax = bool(_EMAIL_RE.match(email))
    domain = ""
    if "@" in email:
        domain = email.rsplit("@", 1)[1].lower()

    out = {
        "email": email,
        "valid_syntax": valid_syntax,
        "domain": domain,
        "has_mx": False,
        "mx_records": [],
    }
    if valid_syntax and domain:
        try:
            res = _doh_resolve(domain, "MX", timeout=timeout)
            out["mx_records"] = res.get("records", [])
            out["mx_status"] = res.get("status")
            out["mx_ttl"] = res.get("ttl", 0)
            out["has_mx"] = bool(out["mx_records"])
            # DoH returns MX as "priority host" e.g. "10 mail.example.com.";
            # surface a parsed view too so callers don't have to split.
            parsed = []
            for rec in out["mx_records"]:
                parts = rec.split(None, 1)
                if len(parts) == 2 and parts[0].isdigit():
                    parsed.append({"priority": int(parts[0]),
                                   "host": parts[1].rstrip(".")})
                else:
                    parsed.append({"priority": None,
                                   "host": rec.rstrip(".")})
            out["mx_parsed"] = parsed
        except ValueError:
            # DoH unreachable — still return the syntax verdict.
            out["mx_error"] = "dns_lookup_failed"
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "email-validate:%s" % domain[:150])
    return jsonify(out)


# ============================================================================
# /api/screenshot — actually fetch a screenshot PNG via a third-party
# headless-browser service (microlink.io free tier).  Unlike the existing
# /api/screenshot-url-hint (which only RETURNs suggested URLs for the caller
# to hit), this endpoint performs the outbound fetch and streams the image
# bytes back, so a client that can only call LinkPeek gets a screenshot.
# Stdlib-only: no headless browser, no Pillow.  Falls back to a 502 + the
# hint payload on any upstream failure so the caller still gets a path.
# ============================================================================
@app.route("/api/screenshot")
@rate_limit(app)
def api_screenshot():
    """Render a screenshot PNG for ?url= via a public screenshot service.

    Query: ?url=https://...               required, target page
    Optional: ?width=1200                 target viewport width (passed to service)
    Optional: ?timeout=15                 upstream fetch timeout, seconds (5..30)

    Returns image/png bytes on success (Content-Type: image/png), or JSON with
    the hint payload + a 502 when the upstream service is unreachable. 400 on a
    bad/missing URL.
    """
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="pass ?url=https://..."), 400
    try:
        timeout = max(5, min(30, int(request.values.get("timeout") or 15)))
    except (ValueError, TypeError):
        timeout = 15
    try:
        width = max(320, min(2400, int(request.values.get("width") or 1200)))
    except (ValueError, TypeError):
        width = 1200
    try:
        url = _normalize_url(url)
    except ValueError as e:
        return jsonify(error=str(e)), 400

    encoded = urlquote(url, safe="")
    # microlink.io: ?screenshot returns PNG bytes when meta=false & element=false.
    shot_url = ("https://api.microlink.io/?url=%s&screenshot&meta=false"
                "&element=false&embed=screenshot.url&viewport.device=desktop"
                "&viewport.width=%d" % (encoded, width))

    _UA = "Mozilla/5.0 (compatible; LinkPeek/1.0; +https://github.com/linkpeek)"
    opener = build_opener(ProxyHandler())
    try:
        req = Request(shot_url, headers={"User-Agent": _UA,
                                         "Accept": "image/png, */*"})
        resp = opener.open(req, timeout=timeout)
        data = resp.read(8 * 1024 * 1024)  # cap at 8 MiB
        ctype = (resp.headers.get("Content-Type") or "").lower()
    except HTTPError as e:
        # microlink returns JSON errors on bad/metered requests; surface as 502.
        try:
            detail = e.read(1024).decode("utf-8", "ignore")
        except Exception:
            detail = e.reason
        record_billing(g.meter_key, g.plan, "screenshot-fail:%s" % url[:150])
        return jsonify(error="upstream_http_%s" % e.code, detail=detail,
                       hint=_screenshot_hint(url)), 502
    except _FETCH_EXC as e:
        record_billing(g.meter_key, g.plan, "screenshot-fail:%s" % url[:150])
        return jsonify(error="upstream_unreachable", detail=str(e)[:150],
                       hint=_screenshot_hint(url)), 502

    # Only stream PNG/image bytes; anything else is upstream spam/error HTML.
    if not (ctype.startswith("image/") or data[:8] == b"\x89PNG\r\n\x1a\n"):
        record_billing(g.meter_key, g.plan, "screenshot-fail:%s" % url[:150])
        return jsonify(error="upstream_not_image",
                       content_type=ctype,
                       hint=_screenshot_hint(url)), 502

    record_billing(g.meter_key, g.plan, "screenshot:%s" % url[:150])
    return Response(data, mimetype="image/png",
                    headers={"Content-Type": "image/png",
                             "X-LinkPeek-Source": "microlink.io",
                             "Cache-Control": "public, max-age=3600"})


# ============================================================================
# /api/page-weight (1.9.0) — estimate total page resource weight from HTML
# ============================================================================
# Fetches the HTML at ?url=, scans for <img src>, <script src>, <link rel=stylesheet
# href>, <iframe src>, <source srcset>, <video src>, <audio src>, and probes each
# distinct, same-origin-or-cross-origin resource with a lightweight request that
# reads only the response headers (Content-Length). Sums the HTML size + all
# resource Content-Length values into an estimated transfer size. The probe is a
# GET (many CDNs omit Content-Length on HEAD) capped at 30 resources with a 5s
# timeout each via a thread pool so the endpoint stays well under the request
# budget. stdlib-only, reuses _fetch + _normalize_url + rate_limit.
# ============================================================================
@app.route("/api/page-weight")
@rate_limit(app)
def api_page_weight():
    """Estimate total page resource weight (bytes) for a URL.

    Query: ?url=https://...   (required)
    Optional: ?limit=30       cap on probed resources (1..60, default 30)
    Optional: ?timeout=5      per-resource probe timeout seconds (2..10, default 5)

    Returns: url, html_bytes, resources (list of {url, type, bytes, status}),
    resource_count, total_bytes (html + resources with a known Content-Length),
    total_bytes_approx (total_bytes counting unknown sizes as 0),
    unknown_count (resources with no Content-Length). 502 on HTML fetch failure,
    400 on a bad/missing URL.
    """
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="pass ?url=https://..."), 400
    try:
        limit = max(1, min(60, int(request.values.get("limit") or 30)))
        per_timeout = float(max(2, min(10, int(request.values.get("timeout") or 5))))
    except ValueError:
        limit, per_timeout = 30, 5.0
    try:
        url = _normalize_url(url)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    try:
        final_url, html_text, _ = _fetch(url)
    except _FETCH_EXC as e:
        return jsonify(url=url, error="fetch_failed: %s" % type(e).__name__), 502

    html_bytes = len(html_text.encode("utf-8", errors="ignore"))
    # Resource tag -> type label. srcset (responsive images) is parsed for the
    # first candidate URL. We only probe http(s) absolute or page-resolved URLs.
    _RE_TO_TYPE = (
        (re.compile(r'<img[^>]*\bsrc=["\']([^"\']+)["\']', re.I), "image"),
        (re.compile(r'<script[^>]*\bsrc=["\']([^"\']+)["\']', re.I), "script"),
        (re.compile(r'<link[^>]*\brel=["\']stylesheet["\'][^>]*\bhref=["\']([^"\']+)["\']', re.I), "stylesheet"),
        (re.compile(r'<link[^>]*\bhref=["\']([^"\']+)["\'][^>]*\brel=["\']stylesheet["\']', re.I), "stylesheet"),
        (re.compile(r'<iframe[^>]*\bsrc=["\']([^"\']+)["\']', re.I), "iframe"),
        (re.compile(r'<video[^>]*\bsrc=["\']([^"\']+)["\']', re.I), "media"),
        (re.compile(r'<audio[^>]*\bsrc=["\']([^"\']+)["\']', re.I), "media"),
    )
    _SRCSET_RE = re.compile(r'srcset=["\']([^"\']+)["\']', re.I)
    _SOURCE_SRC_RE = re.compile(r'<source[^>]*\bsrc=["\']([^"\']+)["\']', re.I)

    base = final_url
    raw_candidates = []
    for rx, typ in _RE_TO_TYPE:
        for m in rx.finditer(html_text):
            raw_candidates.append((m.group(1).strip(), typ))
    for m in _SOURCE_SRC_RE.finditer(html_text):
        raw_candidates.append((m.group(1).strip(), "media"))
    # srcset: first candidate only, "url 2x" form.
    for m in _SRCSET_RE.finditer(html_text):
        first = m.group(1).split(",")[0].strip().split()[0]
        if first:
            raw_candidates.append((first, "image"))

    # De-dup by resolved absolute URL, keep type of first occurrence.
    seen = set()
    resources = []
    for raw, typ in raw_candidates:
        try:
            absu = urljoin(base, raw)
        except Exception:
            continue
        if not absu:
            continue
        s = urlsplit(absu)
        if s.scheme not in _ALLOWED_SCHEMES:
            continue
        if absu in seen:
            continue
        seen.add(absu)
        resources.append({"url": absu, "type": typ})
        if len(resources) >= limit:
            break

    _UA = "Mozilla/5.0 (compatible; LinkPeek/1.0; +https://github.com/linkpeek)"
    opener = build_opener(ProxyHandler())

    def _probe(res):
        try:
            req = Request(res["url"], headers={"User-Agent": _UA,
                                              "Accept": "*/*;q=0.8",
                                              "Accept-Encoding": "gzip, deflate"},
                          method="GET")
            r = opener.open(req, timeout=per_timeout)
            r.read(1)  # trigger headers
            clen = r.headers.get("Content-Length") or r.headers.get("content-length")
            status = r.getcode() or 200
        except HTTPError as e:
            clen = e.headers.get("Content-Length") if e.headers else None
            status = e.code
        except _FETCH_EXC:
            clen = None
            status = 0
        try:
            b = int(clen) if clen is not None else None
        except (TypeError, ValueError):
            b = None
        res["bytes"] = b
        res["status"] = status
        return res

    out_res = []
    if resources:
        with ThreadPoolExecutor(max_workers=min(10, len(resources))) as ex:
            for r in ex.map(_probe, resources):
                out_res.append(r)

    total_known = html_bytes
    unknown = 0
    for r in out_res:
        if r.get("bytes") is not None:
            total_known += r["bytes"]
        else:
            unknown += 1
    out = {
        "url": final_url,
        "html_bytes": html_bytes,
        "resource_count": len(out_res),
        "resources": out_res,
        "total_bytes": total_known,
        "total_bytes_approx": total_known,
        "unknown_count": unknown,
    }
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "page-weight:%s" % final_url[:150])
    return jsonify(out)


# ============================================================================
# /api/lighthouse-hint (1.9.0) — static Core Web Vitals hints from raw HTML
# ============================================================================
# A stdlib-only "lite Lighthouse": fetches ?url= and, WITHOUT a headless
# browser, estimates the three Core Web Vitals (LCP/FID-INP/CLS) signals that
# can be inferred from markup alone — render-blocking CSS/JS, image sizing
# (width/height attributes / srcset / lazy-loading), DOM size, webfont @font-face
# usage, and cumulative layout shift risks (images without explicit dimensions).
# Returns a scored checklist + an estimated Lighthouse-style 0-100
# performance_score derived from weighted sub-signals. This is a hint, not a
# measured lab/run — the docstring and `lab_run: false` field make that clear.
# ============================================================================
@app.route("/api/lighthouse-hint")
@rate_limit(app)
def api_lighthouse_hint():
    """Static Core Web Vitals / performance hints parsed from page HTML.

    Query: ?url=https://...   (required)

    Returns: url, dom_size, render_blocking (count + examples of CSS/JS in
    <head>), images (total, with_dimensions, lazy_loaded, srcset, layout_shift
    risk), fonts (count of @font-face), scripts (total, async, defer,
    in_head), lab_run=false, signals (named sub-checks raw/ok), weighted
    performance_score (0-100 heuristic, NOT a real Lighthouse run). 502 on fetch
    failure, 400 on a bad/missing URL.
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

    head = _extract_head(html_text)

    # DOM size: count opening tags (rough). Capped at a sane int.
    dom_size = len(re.findall(r"<[a-zA-Z][^>]*>", html_text))

    # Render-blocking CSS in <head>: <link rel=stylesheet> not media=print.
    css_blocks = 0
    css_examples = []
    for m in re.finditer(r'<link[^>]*rel=["\']stylesheet["\'][^>]*>', html_text, re.I):
        tag = m.group(0)
        if 'media="print"' in tag.lower() or "media='print'" in tag.lower():
            continue
        css_blocks += 1
        if len(css_examples) < 5:
            href = re.search(r'\bhref=["\']([^"\']+)["\']', tag, re.I)
            css_examples.append(href.group(1) if href else tag[:80])
        else:
            css_examples.append("")  # cap list

    # Render-blocking sync <script> in <head> (no async/defer).
    head_scripts = re.findall(r'<script[^>]*>', head, re.I)
    sync_in_head = 0
    for t in head_scripts:
        if "async" in t.lower() or "defer" in t.lower():
            continue
        if 'type="module"' in t.lower() or "type='module'" in t.lower():
            continue
        sync_in_head += 1

    # Images: total, with width or height attr, lazy-loaded (loading=lazy),
    # with srcset, and layout-shift risk (images WITHOUT explicit width&height).
    img_tags = re.findall(r'<img[^>]*>', html_text, re.I)
    img_total = len(img_tags)
    img_with_dims = 0
    img_lazy = 0
    img_srcset = 0
    img_shift_risk = 0
    for t in img_tags:
        low = t.lower()
        has_w = bool(re.search(r'\bwidth=', low))
        has_h = bool(re.search(r'\bheight=', low))
        if has_w and has_h:
            img_with_dims += 1
        else:
            img_shift_risk += 1
        if "loading=" in low and ("lazy" in low):
            img_lazy += 1
        if "srcset=" in low:
            img_srcset += 1

    fonts = len(re.findall(r"@font-face", html_text, re.I))

    all_scripts = re.findall(r'<script[^>]*>', html_text, re.I)
    total_scripts = len(all_scripts)
    async_count = sum(1 for t in all_scripts if "async" in t.lower())
    defer_count = sum(1 for t in all_scripts if "defer" in t.lower())

    # Heuristic sub-signals (each: 1 = good, 0 = risky).
    signals = {
        # Render-blocking: penalised when CSS in <head> > 2 or sync head scripts.
        "render_blocking_low": css_blocks <= 2 and sync_in_head == 0,
        # Images sized (CLS proxy): majority have width+height.
        "images_sized": img_with_dims >= img_total * 0.6 if img_total else True,
        # Lazy loading present: at least one lazy image (LCP friendliness proxy).
        "lazy_loading_used": img_lazy > 0,
        # Responsive images: srcset used.
        "responsive_images": img_srcset > 0,
        # Small DOM (< ~1500 nodes Lighthouse target).
        "dom_size_small": dom_size < 1500,
        # Few webfonts (each font adds layout shift / fetch cost).
        "fonts_few": fonts <= 2,
        # Scripts deferred/async (INP/FID friendliness proxy).
        "scripts_non_blocking": (async_count + defer_count) >= total_scripts * 0.5 if total_scripts else True,
    }
    weighted = {
        "render_blocking_low": 0.25,
        "images_sized": 0.25,
        "lazy_loading_used": 0.10,
        "responsive_images": 0.10,
        "dom_size_small": 0.15,
        "fonts_few": 0.05,
        "scripts_non_blocking": 0.10,
    }
    perf_score = round(sum(weighted[k] for k, v in signals.items() if v) * 100)

    out = {
        "url": final_url,
        "lab_run": False,
        "note": "Static hints parsed from HTML markup only; not a measured Lighthouse lab run.",
        "dom_size": dom_size,
        "render_blocking": {
            "css_count": css_blocks,
            "sync_scripts_in_head": sync_in_head,
            "css_examples": [e for e in css_examples if e][:5],
        },
        "images": {
            "total": img_total,
            "with_dimensions": img_with_dims,
            "lazy_loaded": img_lazy,
            "with_srcset": img_srcset,
            "layout_shift_risk_count": img_shift_risk,
        },
        "fonts": fonts,
        "scripts": {
            "total": total_scripts,
            "async": async_count,
            "defer": defer_count,
        },
        "signals": signals,
        "performance_score": perf_score,
    }
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "lighthouse-hint:%s" % final_url[:150])
    return jsonify(out)


# ============================================================================
# /api/json-validate (1.9.2) — validate + summarize a JSON string or fetch+validate a JSON URL
# ============================================================================
# POST body or ?json=<raw> validates JSON syntax without any third-party lib.
# If ?url= is given instead, fetches that URL (must be JSON Content-Type) and
# validates the body. Useful for link-preview consumers that ingest JSON-LD
# payloads and want a quick "is this well-formed + what shape" check. Reuses
# rate_limit + _normalize_url + _fetch. No new deps. Capped at 256 KiB of input
# to keep the endpoint cheap.
_MAX_JSON = 256 * 1024


@app.route("/api/json-validate", methods=["GET", "POST"])
@rate_limit(app)
def api_json_validate():
    """Validate a JSON payload and report a structural summary.

    Query/Body:
        ?json=<raw JSON string>   inline payload to validate (any method)
        ?url=https://...          fetch a JSON document and validate its body
        (exactly one of json/url is required; json takes precedence)

    Returns: valid (bool), size_bytes, type ("object"/"array"/"number"/...),
    keys (top-level keys when an object), length (for arrays), error, plus the
    quota echo. 400 on a missing input, 422 on invalid JSON, 502 on fetch fail.
    """
    raw = None
    if request.method == "POST" and request.data:
        raw = request.get_data(as_text=True)
    if raw is None:
        raw = (request.values.get("json") or "").strip()
    url = (request.values.get("url") or "").strip()

    if not raw and not url:
        return jsonify(error="pass ?json=... or ?url=..."), 400

    source = "inline"
    if raw:
        if len(raw) > _MAX_JSON:
            return jsonify(valid=False, error="payload_too_large",
                           max_bytes=_MAX_JSON, got=len(raw)), 413
        text = raw
    else:
        # Fetch the URL and validate the response body.
        try:
            url = _normalize_url(url)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        try:
            _final, html_text, hdrs = _fetch(url, timeout=10)
        except _FETCH_EXC as e:
            return jsonify(url=url, error="fetch_failed: %s" % type(e).__name__), 502
        ctype = (hdrs.get("Content-Type") or "").lower()
        if "json" not in ctype:
            return jsonify(url=url, valid=False,
                           error="not_json_content_type", content_type=ctype), 422
        if len(html_text.encode("utf-8", "ignore")) > _MAX_JSON:
            return jsonify(url=url, valid=False, error="payload_too_large",
                           max_bytes=_MAX_JSON), 413
        text = html_text
        source = url

    try:
        parsed = json.loads(text)
    except (ValueError, TypeError) as e:
        record_billing(g.meter_key, g.plan, "json-validate:invalid")
        return jsonify(valid=False, source=source,
                       size_bytes=len(text.encode("utf-8", "ignore")),
                       error="invalid_json: %s" % str(e)[:200]), 422

    out = {
        "valid": True,
        "source": source,
        "size_bytes": len(text.encode("utf-8", "ignore")),
        "type": type(parsed).__name__,
    }
    if isinstance(parsed, dict):
        out["keys"] = sorted(parsed.keys())[:100]
        out["key_count"] = len(parsed)
    elif isinstance(parsed, list):
        out["length"] = len(parsed)
        # Sample the type of the first element to hint at list shape.
        if parsed:
            out["first_element_type"] = type(parsed[0]).__name__
    record_billing(g.meter_key, g.plan, "json-validate:ok")
    out["quota"] = quota_echo(g)
    return jsonify(out)


# ============================================================================
# /api/social-embed (1.9.2) — single-call "ready-to-paste" embed bundle.
# ============================================================================
# Combines OpenGraph, Twitter Card, and favicon into one consumer-facing dict
# suitable for link-card / chat-preview UIs. Saves a client from calling
# /api/opengraph + /api/meta-tags + /api/favicons and stitching the result.
@app.route("/api/social-embed")
@rate_limit(app)
def api_social_embed():
    """Build a single 'ready-to-paste' social embed object for a URL.

    Query: ?url=https://...   (required)

    Returns: url, title, description, image, site_name, favicon, plus
    ``cards`` (twitter:card / twitter:title / twitter:description /
    twitter:image when present) and a ``best_image`` (twitter:image preferred
    over og:image for higher-res social crops). All field sources are the page
    meta tags; no fetches beyond the single HTML pull. 400 on bad URL, 502 on
    fetch failure.  Mirrors /api/opengraph metering + quota echo.
    """
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="pass ?url=https://..."), 400
    try:
        url = _normalize_url(url)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    try:
        # collect_body=True so the result includes the full 'meta' dict
        # (parser.meta holds twitter:* + og:*); without it the Twitter Card
        # fields would all resolve to empty strings.
        out = preview_link(url, collect_body=True)
    except _FETCH_EXC as e:
        return jsonify(url=url, error="fetch_failed: %s" % type(e).__name__), 502

    meta = out.get("meta", {})
    cards = {
        "twitter:card": meta.get("twitter:card", ""),
        "twitter:title": meta.get("twitter:title", ""),
        "twitter:description": meta.get("twitter:description", ""),
        "twitter:image": meta.get("twitter:image", ""),
    }
    # Prefer twitter:image when present (often higher-res than og:image).
    best_image = cards["twitter:image"] or out.get("image", "")
    if best_image:
        best_image = urljoin(out.get("url", ""), best_image)
    if cards["twitter:image"]:
        cards["twitter:image"] = urljoin(out.get("url", ""),
                                         cards["twitter:image"])
    bundle = {
        "url": out.get("url", ""),
        "title": _clean(out.get("title", "")),
        "description": _clean(out.get("description", "")),
        "image": best_image,
        "site_name": _clean(out.get("site_name", "")),
        "favicon": out.get("favicon", ""),
        "cards": cards,
        "source": "meta",
    }
    bundle["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "social-embed:%s" % url[:150])
    return jsonify(bundle)


# ============================================================================
# /api/whois-lookup — domain registration data via RDAP (the RESTful successor
# to WHOIS). Returns registrar, created/updated/expiry dates, name servers,
# domain status, and DNSSEC flag. Complements /api/dns-lookup and /api/ssl-info
# for a full "tell me everything about this domain" toolkit. Uses the IANA
# bootstrap (rdap.org redirects to the authoritative RDAP server) — stdlib
# only, no third-party library, no API key, no rate-limit cost.
# ============================================================================
@app.route("/api/whois-lookup")
@rate_limit(app)
def api_whois_lookup():
    """RDAP-based domain registration lookup.

    Query: ?domain=example.com  (required; bare hostname or URL — we strip
                                  scheme/path/port the same way /dns-lookup does)

    Returns: domain, registrar, created, updated, expires, status[], secureDNS,
    nameservers[], handle, rdap_source. 400 on a bad/missing domain, 502 when
    the RDAP server is unreachable, 404 when the domain is not registered
    (RDAP returns 404 for available/unregistered names).
    """
    raw = (request.values.get("domain") or request.values.get("url") or "").strip()
    if not raw:
        return jsonify(error="pass ?domain=example.com"), 400
    # Reuse the same host-extraction logic as /api/dns-lookup so the two
    # endpoints accept identical input shapes.
    if "://" in raw:
        parts = urlsplit(raw)
        domain = parts.hostname or ""
    else:
        candidate = raw.split("/", 1)[0]
        if candidate.startswith("["):
            end = candidate.find("]")
            domain = candidate[1:end] if end != -1 else candidate
        else:
            domain = candidate.rsplit(":", 1)[0] if candidate.count(":") == 1 else candidate
    domain = domain.lower().strip(".")
    if not domain or not re.match(r"^[a-z0-9.\-]+$", domain):
        return jsonify(error="invalid_domain", domain=raw), 400
    if len(domain) > 253:
        return jsonify(error="domain_too_long", max=253, got=len(domain)), 400

    rdap_url = "https://rdap.org/domain/" + urlquote(domain, safe="")
    opener = build_opener(ProxyHandler())
    req = Request(
        rdap_url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; LinkPeek/1.0; +https://github.com/linkpeek)",
            "Accept": "application/rdap+json",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        },
        method="GET",
    )
    try:
        resp = opener.open(req, timeout=12.0)
        raw_bytes = resp.read(256 * 1024)  # RDAP responses can be large; cap defensively
        rdap_resp_headers = {k: v for k, v in resp.headers.items()}
        payload = json.loads(_decode(raw_bytes, rdap_resp_headers))
    except HTTPError as e:
        if e.code == 404:
            return jsonify(domain=domain, registered=False, note="domain appears unregistered (RDAP 404)"), 404
        return jsonify(domain=domain, error="rdap_error", status=e.code), 502
    except (URLError, socket.timeout, TimeoutError, ConnectionError, OSError,
            ssl.SSLError, ValueError) as exc:
        return jsonify(domain=domain, error="rdap_failed: %s" % type(exc).__name__), 502

    # Entities: the one with role "registrar" is who sold the name.
    registrar = ""
    for ent in payload.get("entities", []) or []:
        if "registrar" in (ent.get("roles") or []):
            # vcardArray is the structured answer; prefer the fn (full name).
            vcard = ent.get("vcardArray") or []
            if len(vcard) >= 2 and isinstance(vcard[1], list):
                for field in vcard[1]:
                    if isinstance(field, list) and field and field[0] == "fn":
                        registrar = field[3] if len(field) > 3 else ""
                        break
            if not registrar:
                registrar = ent.get("handle", "")
        if registrar:
            break

    # Events: registration / expiration / last changed dates.
    created = updated = expires = ""
    for ev in payload.get("events", []) or []:
        action = ev.get("eventAction", "")
        date = ev.get("eventDate", "")
        if action == "registration":
            created = date
        elif action == "expiration":
            expires = date
        elif action in ("last changed", "last update of RDAP database"):
            if not updated:
                updated = date

    nameservers = [
        ns.get("ldhName", "").lower() for ns in payload.get("nameservers", []) or []
        if ns.get("ldhName")
    ]
    secure_dns = payload.get("secureDNS", {}) or {}

    out = {
        "domain": domain,
        "registered": True,
        "registrar": registrar,
        "created": created,
        "updated": updated,
        "expires": expires,
        "status": payload.get("status", []) or [],
        "secureDNS": {
            "delegationSigned": bool(secure_dns.get("delegationSigned", False)),
            "zoneSigned": bool(secure_dns.get("zoneSigned", False)),
        },
        "nameservers": nameservers,
        "handle": payload.get("handle", ""),
        "rdap_source": "rdap.org",
    }
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "whois:%s" % domain[:150])
    return jsonify(out)


# ============================================================================
# /api/spf-check — parse + validate a domain's SPF and DMARC DNS records.
# Surfaces misconfigured email-authentication that hurts deliverability.
# Reuses _doh_resolve (already used by /api/dns-lookup and /api/email-validate)
# so it's pure stdlib HTTPS to dns.google. Complements /api/email-validate
# (which checks ONE address's MX); this checks the domain's auth posture.
# ============================================================================
def _parse_spf(spf_record: str) -> dict:
    """Split an SPF TXT value into structured mechanisms + qualifiers."""
    if not spf_record.startswith("v=spf1"):
        return {"raw": spf_record, "valid": False, "error": "not an spf record"}
    # v=spf1 ... ends with a qualifier-all term. Tokens after the version.
    tokens = spf_record[len("v=spf1"):].strip().split()
    mechanisms: list = []
    qual_all = ""
    dns_lookups = 0
    for tok in tokens:
        # Strip an optional qualifier prefix before checking the mechanism
        # name, so that ``+all``, ``-all``, ``~all``, ``?all`` and bare
        # ``all`` are all recognised as the ``all`` mechanism. The original
        # code only checked ``tok.lower().startswith("all")`` which silently
        # missed every qualified form (``+all``, ``-all``, ``~all``, ``?all``),
        # leaving ``all_qualifier`` empty and causing the return expression
        # ``qual_all or "neutral"`` to mis-report every qualifier as neutral.
        qual_char = tok[0] if tok[0] in "+-~?" else "+"
        mech_name = tok[1:] if tok[0] in "+-~?" else tok
        qual_map = {"+": "pass", "-": "fail", "~": "softfail", "?": "neutral"}
        qual = qual_map.get(qual_char, "pass")
        if mech_name.lower() == "all":
            qual_all = qual
            mechanisms.append({"mechanism": "all", "qualifier": qual})
            continue
        # Count include: redirect= as DNS lookups (RFC 7208 §11.1 caps at 10).
        if mech_name.lower().startswith("include:") or mech_name.lower().startswith("redirect="):
            dns_lookups += 1
        mechanisms.append({"mechanism": mech_name, "qualifier": qual})
    return {
        "raw": spf_record,
        "valid": True,
        "all_qualifier": qual_all or "neutral",
        "mechanisms": mechanisms,
        "dns_lookups": dns_lookups,
        "lookup_limit_exceeded": dns_lookups > 10,
    }


def _parse_dmarc(dmarc_record: str) -> dict:
    """Split a v=DMARC1 tag string into a structured dict."""
    if not dmarc_record.lower().startswith("v=dmarc1"):
        return {"raw": dmarc_record, "valid": False, "error": "not a dmarc record"}
    tags: dict = {}
    for pair in dmarc_record.split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        tags[k.strip().lower()] = v.strip()
    return {
        "raw": dmarc_record,
        "valid": True,
        "policy": tags.get("p", ""),
        "subdomain_policy": tags.get("sp", ""),
        "pct": tags.get("pct", ""),
        "aggregate_reports": tags.get("rua", ""),
        "forensic_reports": tags.get("ruf", ""),
        "alignment_dkim": tags.get("adkim", ""),
        "alignment_spf": tags.get("aspf", ""),
        "tags": tags,
    }


@app.route("/api/spf-check")
@rate_limit(app)
def api_spf_check():
    """Inspect + parse a domain's SPF and DMARC DNS records.

    Query: ?domain=example.com  (required)

    Returns: domain, spf {found, ...parsed}, dmarc {found, ...parsed},
    recommendations[] (actionable warnings: missing records, lookup-limit
    breach, none/quarantine DMARC policy, missing ~all). Uses DoH (dns.google)
    reusing the /api/dns-lookup helper — no new deps. 400 on bad domain, 502
    if DoH is unreachable.
    """
    raw = (request.values.get("domain") or request.values.get("url") or "").strip()
    if not raw:
        return jsonify(error="pass ?domain=example.com"), 400
    if "://" in raw:
        parts = urlsplit(raw)
        domain = parts.hostname or ""
    else:
        candidate = raw.split("/", 1)[0]
        if candidate.startswith("["):
            end = candidate.find("]")
            domain = candidate[1:end] if end != -1 else candidate
        else:
            domain = candidate.rsplit(":", 1)[0] if candidate.count(":") == 1 else candidate
    domain = domain.lower().strip(".")
    if not domain or not re.match(r"^[a-z0-9.\-]+$", domain):
        return jsonify(error="invalid_domain", domain=raw), 400
    if len(domain) > 253:
        return jsonify(error="domain_too_long", max=253, got=len(domain)), 400

    # Per-record DoH timeout (seconds). Fixed at 6s which matches the default
    # in _doh_resolve; the old code wrapped a bare ``timeout = 6`` assignment in
    # a try/except ValueError that could never fire (int literal, no call) —
    # dead noise left over from a refactored ``int(request.values.get(...))``.
    timeout = 6

    # --- SPF (TXT on the apex domain) ---
    spf_record = ""
    spf_error = ""
    try:
        res = _doh_resolve(domain, "TXT", timeout=timeout)
        for rec in res.get("records", []):
            if rec.lower().startswith("v=spf1"):
                spf_record = rec
                break
    except ValueError as exc:
        spf_error = str(exc)

    spf_out = {"found": bool(spf_record)}
    if spf_record:
        spf_out.update(_parse_spf(spf_record))
    elif spf_error:
        spf_out["error"] = spf_error

    # --- DMARC (TXT on _dmarc.<domain>) ---
    dmarc_record = ""
    dmarc_error = ""
    try:
        res = _doh_resolve("_dmarc." + domain, "TXT", timeout=timeout)
        for rec in res.get("records", []):
            if rec.lower().startswith("v=dmarc1"):
                dmarc_record = rec
                break
    except ValueError as exc:
        dmarc_error = str(exc)

    dmarc_out = {"found": bool(dmarc_record)}
    if dmarc_record:
        dmarc_out.update(_parse_dmarc(dmarc_record))
    elif dmarc_error:
        dmarc_out["note"] = "no DMARC record (DNS error: %s)" % dmarc_error

    # --- Recommendations ---
    recs: list = []
    if not spf_record:
        recs.append("No SPF record found — email from this domain may be spoofed. Publish a TXT \"v=spf1 ...\" record.")
    elif spf_out.get("lookup_limit_exceeded"):
        recs.append("SPF exceeds the 10-DNS-lookup limit (RFC 7208 §11.1) — receivers may treat as PermError.")
    elif spf_out.get("all_qualifier") == "pass":
        recs.append("SPF ends with '+all' (pass) — anyone can send on behalf of this domain. Use '~all' (softfail) or '-all' (fail).")
    if not dmarc_record:
        recs.append("No DMARC record at _dmarc.%s — publish one to receive abuse reports and enforce alignment." % domain)
    elif dmarc_out.get("policy") in ("", "none"):
        recs.append("DMARC policy is 'none' (monitor-only) — upgrade to 'quarantine' or 'reject' once reports look clean.")

    out = {
        "domain": domain,
        "spf": spf_out,
        "dmarc": dmarc_out,
        "recommendations": recs,
    }
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "spf-check:%s" % domain[:150])
    return jsonify(out)


# ============================================================================
# /api/ssl-check — certificate expiry/issuer summary with days-until-expiry.
# Complements /api/ssl-info (which returns the full cert + protocol + cipher)
# by focusing on the actionable question "how soon does this cert expire?"
# Reuses _ssl_probe so it's pure stdlib. Adds a parsed datetime + an expiry
# status bucket (valid / expiring_soon / expired / unknown) so monitoring
# dashboards can alert without parsing RFC 2822 date strings themselves.
# ============================================================================
def _parse_cert_date(date_str: str):
    """Parse an RFC 2822 ``notAfter`` string (e.g. 'Sep 15 23:59:59 2025 GMT').

    Returns a timezone-aware UTC datetime, or None on failure. Uses
    email.utils.parsedate_to_datetime which handles the exact format
    ssl.getpeercert() emits.
    """
    if not date_str:
        return None
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(date_str)
        if dt is None:
            return None
        # parsedate_to_datetime may return a naive datetime if no tzinfo was
        # present in the string; normalise to UTC so subtraction is safe.
        if dt.tzinfo is None:
            import datetime as _dt
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt
    except (TypeError, ValueError, OverflowError):
        return None


@app.route("/api/ssl-check")
@rate_limit(app)
def api_ssl_check():
    """Certificate expiry + issuer summary for an https URL.

    Query: ?url=https://...  (required; scheme must be https)
    Optional: ?warn_days=30  (threshold for "expiring_soon" status; 1-365, default 30)
    Returns: url, host, port, valid (bool), issuer (dict), subject (dict),
    not_after (raw string), not_after_iso (ISO 8601, or null),
    days_until_expiry (int, or null), expiry_status
    ("valid" | "expiring_soon" | "expired" | "unknown"), warn_days.
    400 on a non-https/bad URL, 502 if the TLS handshake fails.
    """
    import datetime as dt
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="pass ?url=https://..."), 400
    try:
        warn_days = max(1, min(365, int(request.values.get("warn_days") or 30)))
    except (ValueError, TypeError):
        warn_days = 30
    try:
        url = _normalize_url(url)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        return jsonify(error="ssl-check requires an https URL"), 400
    host = parts.hostname or ""
    if not host:
        return jsonify(error="invalid host"), 400
    port = parts.port or 443
    try:
        probe = _ssl_probe(host, port)
    except _FETCH_EXC as e:
        return jsonify(url=url, error="fetch_failed: %s" % type(e).__name__), 502

    not_after_raw = probe.get("not_after", "")
    not_after_dt = _parse_cert_date(not_after_raw)
    now_utc = dt.datetime.now(dt.timezone.utc)
    days_left: int | None = None
    not_after_iso = ""
    if not_after_dt is not None:
        not_after_iso = not_after_dt.isoformat()
        days_left = (not_after_dt - now_utc).days

    # Expiry status bucket for alerting/monitoring.
    if not probe["valid"]:
        expiry_status = "unknown"  # we couldn't get a parsed cert
    elif days_left is None:
        expiry_status = "unknown"
    elif days_left < 0:
        expiry_status = "expired"
    elif days_left <= warn_days:
        expiry_status = "expiring_soon"
    else:
        expiry_status = "valid"

    out = {
        "url": url,
        "host": host,
        "port": port,
        "valid": probe["valid"],
        "issuer": probe["issuer"],
        "subject": probe["subject"],
        "not_after": not_after_raw,
        "not_after_iso": not_after_iso or None,
        "days_until_expiry": days_left,
        "expiry_status": expiry_status,
        "warn_days": warn_days,
    }
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "ssl-check:%s" % host[:150])
    return jsonify(out)


# ============================================================================
# /api/security-txt — fetch + parse a domain's RFC 9116 security.txt file.
# security.txt lives at /.well-known/security.txt and standardises how
# security researchers contact a site about vulnerabilities. This endpoint
# fetches it (trying /.well-known/ first, then /security.txt as a fallback),
# parses the key-value directives, and returns a structured dict — so a
# bug-bounty dashboard or compliance tool can check "does this domain have
# a security contact?" in one API call. Pure stdlib, reuses _normalize_url +
# _fetch + _FETCH_EXC.
# ============================================================================
_SEC_TXT_MAX_BYTES = 256 * 1024  # security.txt is small; cap defensively.


@app.route("/api/security-txt")
@rate_limit(app)
def api_security_txt():
    """Fetch and parse a domain's RFC 9116 security.txt.

    Query: ?url=https://...  (required; any http(s) URL — we extract the origin)
    Returns: url, security_txt_url (the final fetched URL), found (bool),
    fields (dict of parsed key→value pairs), contacts[] (all mailto/http URLs
    found in the ``Contact`` field(s)), comments[] (lines starting with #),
    raw (the full text, capped at 4096 chars). 404 if no security.txt is
    found at either well-known location, 502 on fetch failure, 400 on a
    bad/missing URL.
    """
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="pass ?url=https://..."), 400
    try:
        url = _normalize_url(url)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    parts = urlsplit(url)
    origin = "%s://%s" % (parts.scheme, parts.netloc or parts.hostname or "")
    if not parts.netloc:
        return jsonify(error="invalid host"), 400

    # Try the standard well-known location first, then the legacy root path.
    candidates = [
        origin + "/.well-known/security.txt",
        origin + "/security.txt",
    ]
    opener = build_opener(ProxyHandler())
    _UA = "Mozilla/5.0 (compatible; LinkPeek/1.0; +https://github.com/linkpeek)"
    raw_text = ""
    fetched_url = ""
    fetch_error = ""
    for cand in candidates:
        req = Request(
            cand,
            headers={"User-Agent": _UA, "Accept": "text/plain, */*;q=0.8"},
            method="GET",
        )
        try:
            resp = opener.open(req, timeout=8.0)
            raw_bytes = resp.read(_SEC_TXT_MAX_BYTES)
            ctype = (resp.headers.get("Content-Type") or "").lower()
            fetched_url = resp.geturl()
            # Accept the body if it looks like text/plain or is parseable as
            # utf-8; some misconfigured servers serve text/html wrappers.
            raw_text = raw_bytes.decode("utf-8", errors="ignore")
            if raw_text.strip():
                break
        except HTTPError as e:
            # 404/403 just means this candidate doesn't have it; try the next.
            if e.code in (404, 403):
                continue
            fetch_error = "http_%s" % e.code
        except _FETCH_EXC as e:
            fetch_error = "fetch_failed: %s" % type(e).__name__

    if not raw_text.strip():
        out = {
            "url": url,
            "origin": origin,
            "found": False,
            "fields": {},
            "contacts": [],
            "comments": [],
            "raw": "",
            "note": "No security.txt found at /.well-known/security.txt or /security.txt",
        }
        if fetch_error:
            out["error"] = fetch_error
            out["quota"] = quota_echo(g)
            record_billing(g.meter_key, g.plan, "security-txt:%s" % origin[:150])
            return jsonify(out), 502
        out["quota"] = quota_echo(g)
        record_billing(g.meter_key, g.plan, "security-txt:%s" % origin[:150])
        return jsonify(out), 404

    # Parse RFC 9116: line-oriented key: value (comments start with #).
    fields: dict = {}
    contacts: list = []
    comments: list = []
    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            comments.append(stripped[1:].strip())
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if not key:
            continue
        # Multiple Contact fields are common; collect them all.
        if key == "contact":
            contacts.append(value)
        # Later values for the same key overwrite earlier ones (last-wins,
        # which matches RFC 9116 field semantics for single-valued fields).
        fields[key] = value

    out = {
        "url": url,
        "origin": origin,
        "security_txt_url": fetched_url,
        "found": True,
        "fields": fields,
        "contacts": contacts,
        "comments": comments,
        "raw": raw_text[:4096],
        "raw_truncated": len(raw_text) > 4096,
    }
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "security-txt:%s" % origin[:150])
    return jsonify(out)


# ============================================================================
# /api/wayback — Internet Archive Wayback Machine snapshot lookup (1.12.0+)
# ============================================================================
# Queries the free, public archive.org/wayback/available JSON API for the
# closest archived snapshot of a URL. Useful for citation tools, recovering
# dead/broken links, citation longevity, and viewing historical page state.
# Zero budget — the archive.org API needs no key and has a generous rate
# limit. Optional ?timestamp=YYYYMMDDhhmmss asks for the snapshot nearest a
# specific moment; the API returns the closest available. Optional
# ?snapshot_limit=N (1-20, default 5) requests additional recent snapshots
# via a second /wayback/available call is not done here — instead we surface
# the timemap via CDX to give N most-recent captures. Reuses _normalize_url
# for SSRF guard + _FETCH_EXC for uniform error shape. Stdlib only.
@app.route("/api/wayback")
@rate_limit(app)
def api_wayback():
    """Find the most recent (or closest-in-time) Internet Archive snapshot.

    Query: ?url=https://...          (required; any public http/https URL)
    Optional: ?timestamp=YYYYMMDDhhmmss  (ISO-ish, digits-only; find the
                                          snapshot nearest this moment)
    Optional: ?limit=5             number of most-recent snapshots to also
                                   return from the CDX timemap (1-20, default 5)

    Returns: url (requested), archived_snapshots.closest {available, url,
    timestamp, status}, snapshots: [{timestamp, status, url}] (recent captures
    from the CDX API, most-recent first), archive_prefix (linkable base),
    has_archive (bool). 400 on a missing/bad URL, 502 if archive.org is
    unreachable, 404-shaped (has_archive=false) when no snapshot exists.
    """
    url = (request.values.get("url") or "").strip()
    if not url:
        return jsonify(error="pass ?url=https://..."), 400
    try:
        url = _normalize_url(url)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    timestamp = re.sub(r"\D", "", request.values.get("timestamp") or "")
    try:
        limit = max(1, min(20, int(request.values.get("limit") or 5)))
    except (ValueError, TypeError):
        limit = 5

    opener = build_opener(ProxyHandler())
    _UA = "Mozilla/5.0 (compatible; LinkPeek/1.0; +https://github.com/linkpeek)"
    archive_prefix = "https://web.archive.org/web"

    # --- Closest snapshot (official /wayback/available API) ---
    avail_url = "https://archive.org/wayback/available?url=%s" % urlquote(
        url, safe=""
    )
    if timestamp:
        avail_url += "&timestamp=%s" % timestamp
    closest = {"available": False, "url": "", "timestamp": "", "status": ""}
    avail_ok = True
    payload = {}
    try:
        req = Request(avail_url, headers={"User-Agent": _UA,
                                          "Accept": "application/json"}, method="GET")
        resp = opener.open(req, timeout=10.0)
        raw = resp.read(128 * 1024)  # tiny JSON payload; cap defensively
        payload = json.loads(raw.decode("utf-8", errors="ignore"))
    except _FETCH_EXC:
        avail_ok = False  # closest-snapshot lookup failed; try CDX below
    if avail_ok:
        snap = (payload.get("archived_snapshots") or {}).get("closest") or {}
        if snap.get("available"):
            closest = {
                "available": True,
                "url": snap.get("url", ""),
                "timestamp": snap.get("timestamp", ""),
                "status": str(snap.get("status", "")),
            }
        # If ?timestamp= was given but no closest returned, keep available=False.

    # --- Recent snapshots via the CDX API (most-recent-first timemap) ---
    # CDX returns one line per capture: key timestamp original mimetype
    # statuscode digest length — tab-separated. We ask for the last `limit`
    # in reverse-chron order. This complements /wayback/available which only
    # returns the single closest hit, so callers get a small timeline.
    snapshots: list = []
    cdx_ok = True
    cdx_url = (
        "https://web.archive.org/cdx/search/cdx?url=%s"
        "&output=json&limit=%d&fl=timestamp,original,statuscode,digest&"
        "sort=desc" % (urlquote(url, safe=""), limit * 4)
    )
    try:
        req = Request(cdx_url, headers={"User-Agent": _UA,
                                        "Accept": "application/json"}, method="GET")
        resp = opener.open(req, timeout=10.0)
        raw = resp.read(256 * 1024)  # CDX rows are tiny but cap anyway
        cdx_rows = json.loads(raw.decode("utf-8", errors="ignore"))
    except _FETCH_EXC:
        # _FETCH_EXC already includes ValueError (json.loads subsumed by it),
        # so this single clause covers both transport failures and bad JSON.
        cdx_ok = False
        cdx_rows = []
    # First row is the header ["timestamp","original","statuscode","digest"].
    if cdx_ok and isinstance(cdx_rows, list) and len(cdx_rows) > 1:
        for row in cdx_rows[1:][:limit]:
            if not isinstance(row, list) or len(row) < 4:
                continue
            ts = str(row[0] or "")
            original = str(row[1] or "")
            sc = str(row[2] or "")
            if not ts:
                continue
            snapshots.append({
                "timestamp": ts,
                "status": sc,
                # Build the linkable playback URL so callers can open it.
                "url": "%s/%s/%s" % (archive_prefix, ts,
                                     original or url),
            })

    has_archive = closest["available"] or bool(snapshots)
    out = {
        "url": url,
        "archive_prefix": archive_prefix,
        "has_archive": has_archive,
        "archived_snapshots": {"closest": closest},
        "snapshots": snapshots,
        "via": "archive.org",
    }
    if not avail_ok and not cdx_ok:
        out["error"] = "archive_unreachable"
        out["quota"] = quota_echo(g)
        record_billing(g.meter_key, g.plan, "wayback:%s" % url[:150])
        return jsonify(out), 502
    if not has_archive:
        # Both lookups worked but nothing archived. 404-shaped but still 200
        # body (a success that found no data) is friendlier for monitoring
        # tools that key on status codes; include has_archive=false instead.
        out["note"] = "No snapshots found in the Wayback Machine."
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "wayback:%s" % url[:150])
    return jsonify(out)


# ============================================================================
# /api/perf-timing — server-side HTTP performance timing for a URL (1.13.0).
# ============================================================================
# Measures, from the LinkPeek server's perspective, the time-to-first-byte
# (TTFB), total download time, total bytes received, and average throughput
# for a public URL. Reuses _normalize_url for SSRF guard + _FETCH_EXC for
# uniform error shape. Stdlib only — uses urllib opener with a 15s cap. This
# gives a rough "how fast does this site load" signal without installing a
# headless browser or a paid monitoring SaaS. Optional ?timeout= (1..15s).
@app.route("/api/perf-timing")
@rate_limit(app)
def api_perf_timing():
    """Measure server-side TTFB + download time + total bytes for a URL.

    Query: ?url=https://...          (required; any public http/https URL)
    Optional: ?timeout=10           fetch timeout in seconds (1..15, default 10)

    Returns: url, status (HTTP status code or 0 on transport failure),
    ttfb_ms (time to first byte in milliseconds), download_ms (time from first
    byte to EOF), total_ms (TTFB + download), bytes (content length), and
    kbps (average throughput). 400 on a missing/bad URL, 502 on fetch failure.
    """
    raw_url = (request.values.get("url") or "").strip()
    if not raw_url:
        return jsonify(error="pass ?url=https://..."), 400
    try:
        url = _normalize_url(raw_url)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    try:
        timeout = max(1, min(15, int(request.values.get("timeout") or 10)))
    except (ValueError, TypeError):
        timeout = 10

    _UA = "Mozilla/5.0 (compatible; LinkPeek/1.0; +https://github.com/linkpeek)"
    opener = build_opener(ProxyHandler())
    total_bytes = 0
    status = 0
    ttfb_ms = 0.0
    download_ms = 0.0
    try:
        req = Request(url, headers={"User-Agent": _UA}, method="GET")
        t_start = time.time()
        resp = opener.open(req, timeout=timeout)
        status = resp.getcode() or 0
        # Read in chunks so we can capture TTFB (first chunk) separately.
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            if not total_bytes:
                ttfb_ms = (time.time() - t_start) * 1000.0
            total_bytes += len(chunk)
        t_end = time.time()
        if not total_bytes:
            # Empty body: TTFB is the whole time, download is 0.
            ttfb_ms = (t_end - t_start) * 1000.0
        else:
            download_ms = (t_end - t_start) * 1000.0 - ttfb_ms
    except _FETCH_EXC as e:
        out = {"url": url, "error": "fetch_failed", "detail": str(e)[:200],
               "status": status, "bytes": total_bytes,
               "ttfb_ms": round(ttfb_ms, 2), "download_ms": round(download_ms, 2),
               "total_ms": round(ttfb_ms + download_ms, 2)}
        out["quota"] = quota_echo(g)
        record_billing(g.meter_key, g.plan, "perf-timing:%s" % url[:150])
        return jsonify(out), 502

    total_ms = ttfb_ms + download_ms
    kbps = (total_bytes / 1024.0 / (download_ms / 1000.0)) if download_ms > 0 else 0.0
    out = {
        "url": url,
        "status": status,
        "ttfb_ms": round(ttfb_ms, 2),
        "download_ms": round(download_ms, 2),
        "total_ms": round(total_ms, 2),
        "bytes": total_bytes,
        "kbps": round(kbps, 2),
    }
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "perf-timing:%s" % url[:150])
    return jsonify(out)


# ============================================================================
# /api/slugify — URL-safe slug from arbitrary text (1.13.0).
# ============================================================================
# Lowercases, strips non-alphanumerics, collapses runs of separator into single
# hyphens, trims leading/trailing hyphens. Pure stdlib (re). Optional ?sep=
# changes the separator (default "-"), ?maxlen= caps length (default 80, min 1,
# max 200, trims on a hyphen boundary). Useful for generating stable URL slugs,
# anchor IDs, and filenames from titles. No network calls.
@app.route("/api/slugify")
@rate_limit(app)
def api_slugify():
    """Slugify a text string into a URL-safe slug.

    Query: ?text=Hello World!    (required; the text to slugify)
    Optional: ?sep=-             separator character (default "-", must be a
                                single non-alphanumeric character: - _ . ~)
    Optional: ?maxlen=80         maximum slug length (1..200, default 80;
                                truncated at the last separator boundary so
                                the slug never ends mid-word or with a dangling
                                separator)

    Returns: text (original), slug (the slugified string), separator, truncated
    (bool). 400 on a missing text or invalid separator.
    """
    text = request.values.get("text") or ""
    sep = (request.values.get("sep") or "-").strip() or "-"
    if not re.match(r"^[\-_.~]$", sep):
        return jsonify(error="sep must be one of: - _ . ~"), 400
    try:
        maxlen = max(1, min(200, int(request.values.get("maxlen") or 80)))
    except (ValueError, TypeError):
        maxlen = 80

    if not text:
        return jsonify(error="pass ?text=..."), 400

    # Lowercase, replace any run of non-alphanumeric chars with the separator,
    # collapse consecutive separators, then trim.
    raw = text.lower().strip()
    raw = re.sub(r"[^a-z0-9]+", sep, raw)
    # Collapse consecutive separators (in case sep appears doubled after the
    # substitution above, though with a single-char sep this is belt-braces).
    if sep:
        raw = re.sub(re.escape(sep) + r"{2,}", sep, raw)
    raw = raw.strip(sep)

    if not raw:
        # Entirely non-alphanumeric input → empty slug.
        out = {
            "text": text,
            "slug": "",
            "separator": sep,
            "truncated": False,
        }
        out["quota"] = quota_echo(g)
        record_billing(g.meter_key, g.plan, "slugify:%d" % len(text))
        return jsonify(out)

    truncated = False
    if len(raw) > maxlen:
        # Cut on the last separator at or before maxlen so we don't split a word.
        cut = raw[:maxlen].rsplit(sep, 1)[0] if sep in raw[:maxlen] else raw[:maxlen]
        raw = cut.rstrip(sep)
        truncated = True

    out = {
        "text": text,
        "slug": raw,
        "separator": sep,
        "truncated": truncated,
    }
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "slugify:%d" % len(text))
    return jsonify(out)


# ============================================================================
# /api/password-strength — analyze password complexity (1.15.0).
# ============================================================================
# Pure stdlib. Scores on length, character-class diversity (lower, upper,
# digit, symbol), entropy estimate, common-password blacklist, and repeated
# or sequential patterns. Returns a 0-100 score, a strength label, and a list
# of actionable improvement suggestions. No network calls, no password storage
# — the input is never persisted.
_PWD_COMMON = frozenset([
    "password", "123456", "12345678", "qwerty", "abc123", "111111", "123456789",
    "12345", "1234567", "admin", "letmein", "welcome", "monkey", "dragon",
    "master", "login", "princess", "football", "shadow", "sunshine", "trustno1",
    "iloveyou", "000000", "password1", "123123", "654321", "superman", "qazwsx",
    "michael", "baseball", "welcome1", "hello", "charlie", "donald", "passw0rd",
    "123", "1234", "abc", "qwerty123", "1q2w3e4r", "letmein123",
])
_PWD_SYMBOL_RE = re.compile(r"[^a-zA-Z0-9]")
_PWD_SEQ = {"abcdefghijklmnopqrstuvwxyz": 4, "0123456789": 4, "qwertyuiop": 4,
            "asdfghjkl": 4, "zxcvbnm": 4}

def _has_sequence(pw, min_len=4):
    """True if pw contains a keyboard/alphabet run of >= min_len chars."""
    low = pw.lower()
    for seq, ml in _PWD_SEQ.items():
        for i in range(len(seq) - ml + 1):
            if seq[i:i + ml] in low:
                return seq[i:i + ml]
    return None

@app.route("/api/password-strength")
@rate_limit(app)
def api_password_strength():
    """Analyze password strength and return a score + improvement tips.

    Query: ?password=…     (required; the password to analyze)
    Optional: ?maxlen=128   reject passwords longer than this (1..4096)

    Returns: password_length, score (0-100), strength (weak|fair|good|strong),
    entropy_bits, character_classes: {lower,upper,digit,symbol}, has_sequence,
    is_common, suggestions: [...]. 400 on a missing password.
    The password value is never echoed back or stored.
    """
    pw = request.values.get("password") or ""
    if not pw:
        return jsonify(error="pass ?password=..."), 400
    try:
        maxlen = max(1, min(4096, int(request.values.get("maxlen") or 128)))
    except (ValueError, TypeError):
        maxlen = 128
    if len(pw) > maxlen:
        return jsonify(error="password_too_long", max=maxlen, got=len(pw)), 413

    has_lower = bool(re.search(r"[a-z]", pw))
    has_upper = bool(re.search(r"[A-Z]", pw))
    has_digit = bool(re.search(r"[0-9]", pw))
    has_symbol = bool(_PWD_SYMBOL_RE.search(pw))
    classes = sum([has_lower, has_upper, has_digit, has_symbol])

    import math
    pool = 0
    if has_lower:
        pool += 26
    if has_upper:
        pool += 26
    if has_digit:
        pool += 10
    if has_symbol:
        pool += 32
    entropy = round(len(pw) * math.log2(pool), 1) if pool else 0.0

    seq = _has_sequence(pw)
    is_common = pw.lower() in _PWD_COMMON or pw == "12345"

    # Scoring: length + diversity + entropy - penalties
    score = 0
    if len(pw) >= 12:
        score += 25
    elif len(pw) >= 8:
        score += 15
    elif len(pw) >= 4:
        score += 5
    score += classes * 12
    if entropy >= 60:
        score += 20
    elif entropy >= 36:
        score += 12
    elif entropy >= 20:
        score += 5
    if is_common:
        score = min(score, 15)
    if seq:
        score -= 10
    if len(set(pw)) <= 2 and len(pw) > 3:
        score -= 15  # e.g. "aaaaaa", "ababab"
    score = max(0, min(100, score))

    if score < 40:
        strength = "weak"
    elif score < 60:
        strength = "fair"
    elif score < 80:
        strength = "good"
    else:
        strength = "strong"

    suggestions = []
    if len(pw) < 12:
        suggestions.append("Use at least 12 characters.")
    if not has_upper:
        suggestions.append("Add uppercase letters.")
    if not has_lower:
        suggestions.append("Add lowercase letters.")
    if not has_digit:
        suggestions.append("Add digits.")
    if not has_symbol:
        suggestions.append("Add symbols (!@#$…).")
    if is_common:
        suggestions.append("Avoid common passwords.")
    if seq:
        suggestions.append("Avoid sequences like '%s'." % seq)
    if not suggestions:
        suggestions.append("Strong password — looks good!")

    out = {
        "password_length": len(pw),
        "score": score,
        "strength": strength,
        "entropy_bits": entropy,
        "character_classes": {
            "lower": has_lower,
            "upper": has_upper,
            "digit": has_digit,
            "symbol": has_symbol,
        },
        "classes_count": classes,
        "has_sequence": bool(seq),
        "sequence": seq,
        "is_common": is_common,
        "unique_chars": len(set(pw)),
        "suggestions": suggestions,
    }
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "password-strength:%d" % len(pw))
    return jsonify(out)


# ============================================================================
# /api/cron-parser — parse standard 5-field cron expressions (1.15.0).
# ============================================================================
# Converts a cron expression (* * * * *) into a human-readable description,
# a structured field breakdown, and the next N run times. Pure stdlib —
# uses datetime arithmetic, no external cron library.
_CRON_ALIASES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
_CRON_FIELD_NAMES = ["minute", "hour", "day_of_month", "month", "day_of_week"]

def _cron_parse_field(spec, lo, hi):
    """Parse one cron field into a sorted list of valid ints. Supports * , - /."""
    if spec == "*":
        return list(range(lo, hi + 1))
    values = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            if not step_s.isdigit():
                raise ValueError("invalid_step")
            step = int(step_s)
            if step == 0:
                raise ValueError("zero_step")
        if part == "*":
            rlo, rhi = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            rlo, rhi = int(a), int(b)
        else:
            rlo = rhi = int(part)
        if rlo < lo or rhi > hi:
            raise ValueError("out_of_range")
        values.update(range(rlo, rhi + 1, step))
    return sorted(values)

def _cron_next_runs(field_sets, now, n=5, max_iter=525600):
    """Compute the next n run datetimes starting from now (exclusive)."""
    from datetime import timedelta
    runs = []
    cur = now.replace(second=0, microsecond=0)
    count = 0
    while count < n and max_iter > 0:
        max_iter -= 1
        cur = cur + timedelta(minutes=1)
        mdow = (cur.weekday() + 1) % 7  # Mon=0→1 ... Sun=6→0
        match = (cur.minute in field_sets[0] and
                 cur.hour in field_sets[1] and
                 cur.day in field_sets[2] and
                 cur.month in field_sets[3] and
                 mdow in field_sets[4])
        if match:
            runs.append(cur.isoformat())
            count += 1
    return runs

@app.route("/api/cron-parser")
@rate_limit(app)
def api_cron_parser():
    """Parse a standard 5-field cron expression into a description + next runs.

    Query: ?expr=*/5 * * * *      (required; 5-field cron or @alias)
    Optional: ?count=5            number of future run times to compute (1..20)
    Optional: ?tz=UTC             timezone label for display only (no conversion)

    Returns: expr, fields: {minute,hour,...} as arrays, human_readable (string),
    next_runs: [ISO timestamps], alias (if an @preset was used). 400 on bad
    syntax, 422 on out-of-range values.
    """
    import datetime
    expr = (request.values.get("expr") or "").strip()
    if not expr:
        return jsonify(error="pass ?expr=5-field-cron-expr"), 400
    try:
        count = max(1, min(20, int(request.values.get("count") or 5)))
    except (ValueError, TypeError):
        count = 5
    tz_label = (request.values.get("tz") or "UTC").strip()[:50]

    alias_used = None
    low = expr.lower()
    if low in _CRON_ALIASES:
        alias_used = low
        expr = _CRON_ALIASES[low]

    parts = expr.split()
    if len(parts) != 5:
        return jsonify(error="invalid_cron", detail="expected 5 whitespace-separated fields"), 422

    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    field_sets = [None] * 5
    try:
        for i, (field, (lo, hi)) in enumerate(zip(parts, ranges)):
            field_sets[i] = _cron_parse_field(field, lo, hi)
    except ValueError as e:
        return jsonify(error="invalid_cron", detail=str(e)), 422

    _month_names = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    _dow_names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    human = "At "
    # minute
    m = field_sets[0]
    if m == list(range(60)):
        human += "every minute"
    else:
        human += "minute %s" % ", ".join(str(x) for x in m[:10])
        if len(m) > 10:
            human += ", …"
    # hour
    h = field_sets[1]
    if h == list(range(24)):
        human += " of every hour"
    else:
        human += " of hour %s" % ", ".join(str(x) for x in h[:10])
        if len(h) > 10:
            human += ", …"
    human += ","
    # dom
    d = field_sets[2]
    if d == list(range(1, 32)):
        human += " every day"
    else:
        human += " on day %s of the month" % ", ".join(str(x) for x in d[:10])
        if len(d) > 10:
            human += ", …"
    # month
    mo = field_sets[3]
    if mo != list(range(1, 13)):
        names = [_month_names[x] for x in mo if x < 13]
        human += " in %s" % ", ".join(names[:6])
    # dow
    dw = field_sets[4]
    if dw != list(range(7)) and dw != [0, 1, 2, 3, 4, 5, 6]:
        names = [_dow_names[x] for x in dw if x < 7]
        human += " on %s" % ", ".join(names[:7])

    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        next_runs = _cron_next_runs(field_sets, now, n=count)
    except Exception:
        next_runs = []

    out = {
        "expr": " ".join(parts),
        "alias": {"@preset": alias_used} if alias_used else None,
        "fields": dict(zip(_CRON_FIELD_NAMES,
                           [field_sets[i] for i in range(5)])),
        "human_readable": human.strip(),
        "next_runs": next_runs,
        "timezone": tz_label,
        "count": len(next_runs),
    }
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, "cron-parser:%s" % expr[:150])
    return jsonify(out)


# ============================================================================
# /api/qr-with-logo — QR code with an embedded centred logo (1.15.0).
# ============================================================================
# Generates a QR code (ECC automatically raised to H so the logo doesn't
# destroy readability) with an optional centred logo image overlaid. The
# logo can be provided as a URL (?logo=) which is fetched, or omitted for a
# plain high-ECC QR. Returns PNG bytes or JSON base64 via ?format=json.
# Uses qrcode + Pillow; graceful 503 if Pillow is not installed.
@app.route("/api/qr-with-logo")
@rate_limit(app)
def api_qr_with_logo():
    """Generate a QR code with an optional centred logo overlay.

    Query: ?text=…           (required; the data to encode)
    Optional: ?logo=https://…   URL of a square PNG/JPEG logo to embed centred
    Optional: ?size=20      QR box_size (default 20; 5..50)
    Optional: ?ecc=h        error correction (forced to H when a logo is set)
    Optional: ?fg=000000    foreground hex colour (default black)
    Optional: ?bg=FFFFFF    background hex colour (default white)
    Optional: ?format=png   png (raw image, default) or json (base64 envelope)
    Optional: ?logo_ratio=20  logo size as % of QR width (1..40, default 20)

    Returns: PNG image or JSON {ok, text, image: "data:image/png;base64,…"}.
    400 on missing text, 413 if text > 2000 chars, 503 if Pillow unavailable.
    """
    text = (request.values.get("text") or "").strip()
    if not text:
        return jsonify(error="pass ?text=..."), 400
    _QR_MAX_CHARS = 2000
    if len(text) > _QR_MAX_CHARS:
        return jsonify(error="text_too_long", max=_QR_MAX_CHARS, got=len(text)), 413

    logo_url = (request.values.get("logo") or "").strip()
    try:
        box_size = max(5, min(50, int(request.values.get("size") or 20)))
    except (ValueError, TypeError):
        box_size = 20
    try:
        logo_ratio = max(1, min(40, int(request.values.get("logo_ratio") or 20)))
    except (ValueError, TypeError):
        logo_ratio = 20
    fmt = (request.values.get("format") or "png").strip().lower()

    def _hex(name, default):
        v = (request.values.get(name) or "").strip().lstrip("#")
        if not v or not re.match(r"^[0-9a-fA-F]{6}$", v):
            return default
        return "#" + v.lower()
    fg = _hex("fg", "#000000")
    bg = _hex("bg", "#ffffff")

    try:
        import qrcode
        from io import BytesIO
        from PIL import Image
    except ImportError:
        return jsonify(error="qrcode+Pillow required (not installed)"), 503

    # Use high ECC when a logo is present to preserve readability.
    ecc_level = qrcode.constants.ERROR_CORRECT_H if logo_url else \
        qrcode.constants.ERROR_CORRECT_M
    qr = qrcode.QRCode(version=None, error_correction=ecc_level,
                        box_size=box_size, border=4)
    qr.add_data(text)
    qr.make(fit=True)

    try:
        img = qr.make_image(fill_color=fg, back_color=bg).convert("RGBA")
    except Exception as exc:
        return jsonify(error="qr_render_failed", detail=str(exc)[:200]), 503

    # Embed logo if provided
    if logo_url:
        try:
            from urllib.request import urlopen
            logo_resp = urlopen(logo_url, timeout=6)
            logo_bytes = logo_resp.read()
            _LOGO_MAX = 512 * 1024  # 512 KiB cap
            if len(logo_bytes) > _LOGO_MAX:
                return jsonify(error="logo_too_large", max_bytes=_LOGO_MAX), 413
            logo = Image.open(BytesIO(logo_bytes)).convert("RGBA")
            # Compute logo size as a fraction of the QR width.
            qr_w = img.size[0]
            logo_w = int(qr_w * logo_ratio / 100)
            logo_h = int(logo_w * (logo.size[1] / logo.size[0]))
            logo_w = min(logo_w, qr_w // 3)
            logo_h = min(logo_h, qr_w // 3)
            if logo_w > 0 and logo_h > 0:
                logo = logo.resize((logo_w, logo_h), Image.LANCZOS)
                pos = ((qr_w - logo_w) // 2, (qr_w - logo_h) // 2)
                img.paste(logo, pos, mask=logo if logo.mode == "RGBA" else None)
        except Exception as exc:
            return jsonify(error="logo_embed_failed",
                           detail=str(exc)[:200]), 503

    try:
        buf = BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        png_data = buf.getvalue()
    except Exception as exc:
        return jsonify(error="qr_render_failed", detail=str(exc)[:200]), 503

    record_billing(g.meter_key, g.plan, "qr-with-logo:%s" % text[:150])

    if fmt == "json":
        import base64
        b64 = base64.b64encode(png_data).decode("ascii")
        out = {
            "ok": True,
            "text": text,
            "has_logo": bool(logo_url),
            "size_bytes": len(png_data),
            "image": "data:image/png;base64," + b64,
        }
        out["quota"] = quota_echo(g)
        return jsonify(out)
    return Response(png_data, mimetype="image/png")


# ============================================================================
# OpenAI-compatible API surface (stub)
#
# Crawlers/scanners routinely probe /v1/models, /v1/chat/completions, /openai/…,
# /api/v1/…, and /health. Instead of returning bare 404s we serve a faithful
# stub of the OpenAI API shape that (a) advertises LinkPeek's actual endpoints
# as a custom model family, (b) echoes a valid /v1/models list so discovery
# tools classify us, and (c) accepts /v1/chat/completions and returns a helpful
# non-AI completion explaining the real API, so the traffic is *captured*
# rather than wasted. No paid deps, no LLM call — pure description.
# ============================================================================
@app.route("/v1/models")
@app.route("/openai/v1/models")
@app.route("/api/v1/models")
def openai_models():
    """OpenAI-compatible GET /v1/models stub.

    Returns the standard OpenAI ``{object: "list", data: [...]}`` envelope so
    discovery/crawler tools that scan for OpenAI APIs register us as a valid
    provider. Each ``data`` entry describes a LinkPeek capability advertised
    as a pseudo-model id (e.g. ``linkpeek-preview``). This is not a real LLM —
    it is a service catalogue that happens to speak the /v1/models shape.
    """
    now = int(time.time())
    models = [
        {
            "id": "linkpeek-preview",
            "object": "model",
            "created": now,
            "owned_by": "linkpeek",
            "capability": "link-preview extraction — GET /api/preview?url=",
        },
        {
            "id": "linkpeek-metadata",
            "object": "model",
            "created": now,
            "owned_by": "linkpeek",
            "capability": "full metadata dump — GET /api/metadata?url=",
        },
        {
            "id": "linkpeek-qr",
            "object": "model",
            "created": now,
            "owned_by": "linkpeek",
            "capability": "QR code PNG generation — GET /api/qr?text=",
        },
        {
            "id": "linkpeek-tech-stack",
            "object": "model",
            "created": now,
            "owned_by": "linkpeek",
            "capability": "framework/CMS fingerprinting — GET /api/tech-stack?url=",
        },
    ]
    return jsonify(object="list", data=models)


@app.route("/v1/chat/completions", methods=["POST"])
@app.route("/openai/v1/chat/completions", methods=["POST"])
@app.route("/api/v1/chat/completions", methods=["POST"])
def openai_chat_completions():
    """OpenAI-compatible POST /v1/chat/completions stub.

    Accepts the standard request body (model, messages) and returns a valid
    OpenAI chat-completion response envelope whose assistant message is a
    concise, helpful message explaining that LinkPeek is a link-preview / QR
    API, not an LLM, and listing the real REST endpoints. This *captures* the
    automated scanner traffic with a useful response instead of a 404, and is
    harmless (no model is loaded, no cost incurred). Intentionally does NOT
    echo the user's messages back or attempt any real generation.
    """
    import time as _time
    body = {}
    try:
        body = request.get_json(force=True, silent=True) or {}
    except Exception:
        body = {}
    model_req = body.get("model", "linkpeek-preview")
    help_text = (
        "LinkPeek is a link-preview and QR-code REST API, not a chat LLM. "
        "This /v1/chat/completions endpoint is a compatibility stub. "
        "Real endpoints: /api/preview?url=, /api/metadata?url=, /api/qr?text=, "
        "/api/tech-stack?url=, /api/dns-lookup?domain=. "
        "Full catalogue: GET /api/status. Docs: https://linkpeek.dev"
    )
    now = int(_time.time())
    completion = {
        "id": "chatcmpl-linkpeek-%d" % now,
        "object": "chat.completion",
        "created": now,
        "model": model_req,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": help_text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
    return jsonify(completion)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
