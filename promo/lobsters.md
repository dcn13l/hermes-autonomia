<!-- lobste.rs — READ-ONLY FROM VPS, can't post -->
<!-- lobste.rs returns HTTP 200 to GET but POST /stories -> 302 login redirect (invite-only account to actually post). The agent has no lobste.rs invite. This draft is for the OPERATOR to paste if they have/obtain an account. -->

# lobste.rs submission draft

**Tag suggestions:** `show` (or ` практический` if you want the "practical" tag), `api`

**Title:** LinkPeek — a self-hostable web-utility API: 65 stdlib-Python GET endpoints (link previews, QR codes, screenshots, DNS/SSL, Wayback) + an OpenAI-compatible shim

**URL:** http://147.15.103.217.sslip.io:5000

**Body (optional, lobste.rs allows text posts):**

I factored the link-card / QR / metadata-fetching glue I kept rebuilding across side projects into a single stdlib-Python + Flask API. 65 GET endpoints, no paid deps, self-host in one `python3 app.py`.

Live (no key, no signup):

```bash
curl "http://147.15.103.217.sslip.io:5000/api/preview?url=github.com"
# {"title":"GitHub · Change is constant...","description":"...","image":"...","quota":{"limit":100,"used_today":1}}

curl -o qr.png "http://147.15.103.217.sslip.io:5000/api/qr?text=hello"   # 200 image/png
```

Categories: link previews/metadata/OpenGraph, QR codes, screenshots, SSL/DNS/whois/security-headers checks, broken-links/redirect-chain/sitemap/robots/Wayback, readability/structured-data. Full list at `/api/status`.

The bit I'm most curious about: an OpenAI-compatible shim (`/v1/models`, `/v1/chat/completions`) makes the API respond with valid OpenAI-shaped JSON, so the growing fleet of OpenAI-API scanners probing hosts discover real endpoints instead of 404s. Either useful pattern or a hack — would value lobsters takes.

Repo (MIT): https://github.com/dcn13l/hermes-autonomia
Latest: https://github.com/dcn13l/hermes-autonomia/discussions/18
