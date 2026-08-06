#!/usr/bin/env python3
"""
LinkPeek — link preview API + QR code generator.

Single Flask app. Endpoints:
    GET  /                 homepage (serves ./index.html)
    GET  /api/preview      metered link-preview extraction
    GET  /api/qr           generate QR code PNG from ?text=
    GET  /api/key?email=…  issues a 14-day trial API key
    GET  /api/health       {ok, today:{day, count}}
"""

from __future__ import annotations

import os
import re
import socket
import ssl
import gzip
import zlib
import io
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, quote as urlquote
from urllib.request import Request, urlopen, build_opener, ProxyHandler
from urllib.error import URLError, HTTPError

from flask import Flask, jsonify, request, g, send_file, Response

from decorators import rate_limit, quota_echo, issue_trial_key, daily_totals, record_billing

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=BASE_DIR, template_folder=BASE_DIR)


# ============================================================================
# stdlib-only link preview extraction
# ============================================================================
class _PeekParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self._in_title = False
        self._head_over = False
        self.title: str = ""
        self.meta: dict[str, str] = {}
        self.favicon: str = ""

    def _stop_head(self):
        self._in_title = False
        self._head_over = True

    def handle_starttag(self, tag, attrs):
        if self._head_over:
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
            return
        if tag == "meta":
            key = a.get("property") or a.get("name")
            if key:
                key = key.lower()
                if key not in self.meta and a.get("content"):
                    self.meta[key] = a["content"]
        if tag == "link":
            rel = (a.get("rel") or "").lower()
            href = a.get("href")
            if href and ("icon" in rel) and not self.favicon:
                self.favicon = urljoin(self.base_url, href)

    def handle_endtag(self, tag):
        if tag == "head":
            self._stop_head()
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and self._head_over is False:
            self.title += data


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
            "User-Agent": "Mozilla/5.0 (compatible; LinkPeek/1.0; +https://147.15.103.217)",
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
            body = e.read()
        except (OSError, AttributeError):
            pass
        if body:
            headers = {"Content-Type": e.headers.get("Content-Type", "")}
            return (e.url or url, _decode(body, headers), headers)
        raise
    raw = resp.read()
    final_url = resp.geturl()
    headers = {k: v for k, v in resp.headers.items()}
    html_text = _decode(raw, headers)
    return final_url, html_text, headers


def _clean(s: str) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip()


def preview_link(url: str) -> dict:
    if not url:
        raise ValueError("missing url")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", url):
        url = "http://" + url
    final_url, html_text, _ = _fetch(url)
    head = html_text.split("</head>", 1)[0]
    if "</head>" not in html_text.lower():
        head = html_text[:65536]
    parser = _PeekParser(final_url)
    try:
        parser.feed(head)
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
    return {
        "url": final_url,
        "title": title,
        "description": description,
        "image": image,
        "site_name": site_name,
        "favicon": favicon,
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
    if not url or not re.match(r"^\w+://|^\S+\.\S+", url):
        return jsonify(error="pass ?url=https://...")
    try:
        out = preview_link(url)
    except (URLError, HTTPError, socket.timeout, ValueError) as e:
        return jsonify(url=url, error="fetch_failed: %s" % type(e).__name__), 502
    out["quota"] = quota_echo(g)
    record_billing(g.meter_key, g.plan, url[:200])
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
