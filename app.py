#!/usr/bin/env python3
"""
LinkPeek — served at http://147.15.103.217/

Single Flask app object. The /api/preview endpoint extracts title,
description, Open-Graph image and favicon for any URL using **only the
standard library** (urllib.request, html.parser, re). No paid deps.

Billing: every billable endpoint is wrapped with `decorators.rate_limit`,
which meters requests per remote IP (free tier, 100/day) or per API key
(Pro/trial, 50,000/day) and returns 429 once the daily bucket is full.
The view reads `flask.g` so it can echo a `quota` block back to the caller,
matching the live API contract:

    GET /api/preview?url=https://github.com
    -> {
         "url": "https://github.com",
         "title": "GitHub …",
         "description": "…",
         "image": "https://…/…png",
         "site_name": "GitHub",
         "favicon": "https://github.com/fluidicon.png",
         "quota": {"used_today": 1, "limit": 100}
       }

Other endpoints (unchanged contract):
    GET  /                 homepage (serves ./index.html)
    GET  /api/preview      metered link-preview extraction
    GET  /api/key?email=…  issues a 14-day trial API key
    GET  /api/health       {ok, today:{day, count}}  (free-tier aggregate)

This is diff-safe: the public behavior of every existing route is preserved.
The /api/preview already existed on the live server — this file makes it
reconstructable from source since /app.py is not publicly exposed.
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
from urllib.request import (
    Request,
    urlopen,
    build_opener,
    HTTPHandler,
    ProxyHandler,
)
from urllib.error import URLError, HTTPError

from flask import Flask, jsonify, request, g, send_file, abort

# Local billing stub — see decorators.py
from decorators import (
    rate_limit,
    quota_echo,
    issue_trial_key,
    daily_totals,
    record_billing,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=BASE_DIR, template_folder=BASE_DIR)


# ============================================================================
# stdlib-only link preview extraction
# ============================================================================
class _PeekParser(HTMLParser):
    """Collect <title>, meta[name=description], og:* and favicon in one pass.

    We stop as soon as we see </head> — every provider of value puts the
    useful metadata there, so the head is sufficient and keeps us fast on
    huge pages (GitHub, news sites with infinite scroll, etc).
    """

    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self._in_title = False
        self._head_over = False
        self.title: str = ""
        self.meta: dict[str, str] = {}  # name|property -> content
        self.favicon: str = ""

    def _stop_head(self):
        # Once </head> is hit, head is over — finish the micro-parser.
        self._in_title = False
        self._head_over = True

    def handle_starttag(self, tag, attrs):
        if self._head_over:
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "head":
            return
        if tag == "title":
            self._in_title = True
            return
        # meta tags cover description, og:title, og:description, og:image,
        # og:site_name, twitter:image, twitter:title, …
        if tag == "meta":
            key = a.get("property") or a.get("name")
            if key:
                key = key.lower()
                if key not in self.meta and a.get("content"):
                    self.meta[key] = a["content"]
        # favicon: prefer rel=icon / shortcut icon
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

    # overrides on /head to also stop on trailing </html>
    def handle_data_end(self, tag):  # noqa: keep signature symmetric
        pass


def _decode(resp_bytes: bytes, headers) -> str:
    """Best-effort decode honouring Content-Encoding (gzip/deflate) and charset."""
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
    # charset from Content-Type
    charset = "utf-8"
    ctype = (headers.get("Content-Type") or "").lower()
    m = re.search(r"charset=([\w\-]+)", ctype)
    if m:
        charset = m.group(1)
    try:
        return data.decode(charset, errors="ignore")
    except LookupError:
        return data.decode("utf-8", errors="ignore")


def _charset_from_meta(html_head: str) -> str | None:
    m = re.search(
        r'<meta[^>]*charset=[\'"]?([\w\-]+)', html_head, re.IGNORECASE
    )
    return m.group(1) if m else None


def _fetch(url: str, timeout: float = 8.0) -> tuple[str, str, dict]:
    """GET a URL, follow redirects, decode, return (final_url, html_text, headers).

    Uses stdlib urllib only. Sends a desktop UA (some sites serve
    'You need to enable JavaScript to run this app' for blank/bot UAs).
    """
    # Honours HTTP(S)_PROXY from env via ProxyHandler.
    opener = build_opener(ProxyHandler())
    req = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (compatible; LinkPeek/1.0; +https://147.15.103.217)"
            ),
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        },
        method="GET",
    )
    ctx = ssl.create_default_context()
    try:
        resp = opener.open(req, timeout=timeout)
    except HTTPError as e:
        # 4xx/5xx may still carry a parseable <head> (e.g. GitHub 404s) —
        # so attempt to read the body if there is one.
        body = b""
        try:
            body = e.read()
        except (OSError, AttributeError):
            pass
        if body:
            headers = {"Content-Type": e.headers.get("Content-Type", "")}
            try:
                enc = e.headers.get("Content-Encoding")
                if enc:
                    headers["Content-Encoding"] = enc
            except AttributeError:
                pass
            return (e.url or url, _decode(
                body,
                headers,
            ), headers)
        raise
    except URLError:
        raise
    raw = resp.read()
    final_url = resp.geturl()
    headers = {k: v for k, v in resp.headers.items()}
    html_text = _decode(raw, headers)
    # Re-decode if a meta charset conflicts with Content-Type charset.
    mc = _charset_from_meta(html_text[:2048])
    if mc:
        try:
            html_text = raw.decode(mc, errors="ignore")
        except LookupError:
            pass
    return final_url, html_text, headers


def _clean(s: str) -> str:
    """Collapse whitespace. Entities are decoded by _PeekParser (convert_charrefs)."""
    if not s:
        return ""
    # convert_charrefs=True in _PeekParser already resolves &...; entities,
    # including numeric refs, before handle_data sees the text. Anything left
    # raw here is almost always a stray escape sequence inside JS-set text
    # that no link-preview consumer would want anyway.
    return re.sub(r"\s+", " ", s).strip()


def preview_link(url: str) -> dict:
    """Extract preview metadata for an arbitrary URL.

    Returns a dict with keys: url, title, description, image, site_name,
    favicon. Missing fields are empty strings -- never absent, so downstream
    JSON contracts hold.
    """
    # normalise
    if not url:
        raise ValueError("missing url")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", url):
        url = "http://" + url

    final_url, html_text, _ = _fetch(url)
    # Defensive cap: the head has all the useful data; avoid parsing MB of body.
    head = html_text.split("</head>", 1)[0]
    if "</head>" not in html_text.lower():
        # no </head>? fall back to parsing the first 64KB
        head = html_text[:65536]

    parser = _PeekParser(final_url)
    try:
        parser.feed(head)
    except AssertionError:
        # html.parser occasionally asserts on malformed input — the data
        # collected so far is still usable.
        pass

    title = _clean(parser.title)
    description = _clean(
        parser.meta.get("description")
        or parser.meta.get("og:description")
        or parser.meta.get("twitter:description")
    )
    og_title = _clean(
        parser.meta.get("og:title") or parser.meta.get("twitter:title")
    )
    if not title:
        title = og_title
    image = parser.meta.get("og:image") or parser.meta.get("twitter:image") or ""
    if image:
        image = urljoin(final_url, image)
    site_name = _clean(parser.meta.get("og:site_name")) or ""
    favicon = parser.favicon or ""
    # A data: favicon (e.g. "data:,") is useless to callers; fall back to /favicon.ico
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
# Routes
# ============================================================================
@app.route("/")
def home():
    idx = os.path.join(BASE_DIR, "index.html")
    if os.path.exists(idx):
        return send_file(idx)
    return ("<h1>LinkPeek</h1><p>preview API at "
            "<code>/api/preview?url=…</code></p>")


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
    return jsonify(key=key, trial_days=14,
                   note="use ?key=<key> on /api/preview")


@app.route("/api/health")
def api_health():
    return jsonify(ok=True, today=daily_totals())


if __name__ == "__main__":
    # Gunicorn / flask run would replace this in prod; kept runnable for
    # standalone testing and the small VPS that hosts the live instance.
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
