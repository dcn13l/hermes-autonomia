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

from decorators import rate_limit, quota_echo, issue_trial_key, daily_totals, record_billing

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=BASE_DIR, template_folder=BASE_DIR)

# Maximum bytes of HTML we will pull into memory per request.
_MAX_BYTES = 2 * 1024 * 1024  # 2 MiB
# Batch endpoint cap.
_BATCH_MAX = 5
_BATCH_TIMEOUT = 12.0


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

    results = []
    with ThreadPoolExecutor(max_workers=min(len(urls), 5)) as ex:
        future_map = {ex.submit(_one, u): u for u in urls}
        for u in urls:
            fut = next((f for f, v in future_map.items() if v == u), None)
            if fut is None:
                results.append({"url": u, "error": "internal_error"})
            else:
                try:
                    results.append(fut.result(timeout=_BATCH_TIMEOUT))
                except Exception as e:
                    results.append({"url": u, "error": "timeout: %s" % type(e).__name__})

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


@app.route("/api/health")
def api_health():
    return jsonify(ok=True, today=daily_totals())


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
