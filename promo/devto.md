---
title: "LinkPeek: a self-hostable web-utility API with 65 endpoints (link previews, QR codes, screenshots, DNS/SSL checks) — free, no signup"
published: false
description: "One stdlib Python file, Flask, no paid deps. 65 GET endpoints for link cards, QR, screenshots, and web ops. Free 100 req/day without a key; OpenAI-compatible too."
tags: sideproject, api, python, opensource, webdev
canonical_url: https://github.com/dcn13l/hermes-autonomia/discussions/18
---

# LinkPeek — a 65-endpoint web-utility API you can self-host in one `python3 app.py`

I kept rebuilding the same link-card / QR / metadata-fetching glue in side projects, so I factored it into **LinkPeek** — a single stdlib-Python + Flask API with 65 GET endpoints for link previews, QR codes, screenshots, and web ops. **No signup for 100 req/day. Self-hostable in one command.**

## Try it now (no key, no signup)

```bash
# Link preview — real output captured 2026-08-08:
curl "http://147.15.103.217.sslip.io:5000/api/preview?url=github.com"
# {"title":"GitHub · Change is constant. GitHub keeps ...",
#  "description":"Join the world's most widely adopted, AI-powered developer platform ...",
#  "image":"https://images.ctfassets.net/.../GH-Homepage-Universe-img.png",
#  "favicon":"https://github.com/fluidicon.png",
#  "site_name":"GitHub",
#  "quota":{"limit":100,"used_today":1}}

# QR code as PNG:
curl -o qr.png "http://147.15.103.217.sslip.io:5000/api/qr?text=hello"   # 200, image/png

# Quick site audits:
curl "http://147.15.103.217.sslip.io:5000/api/dns-lookup?url=github.com"
curl "http://147.15.103.217.sslip.io:5000/api/ssl-check?url=github.com"
curl "http://147.15.103.217.sslip.io:5000/api/broken-links?url=github.com"
curl "http://147.15.103.217.sslip.io:5000/api/wayback?url=github.com"   # Wayback Machine lookup
```

## What's in the box (65 endpoints)

Full machine-readable list at `GET /api/status` returns:

```
{"ok":true,"service":"linkpeek","free_daily_limit":100,"pro_daily_limit":50000,
 "uptime_seconds":...,"endpoints":[{"path":"/api/preview","methods":["GET"]}, ...]}
```

By category:

- **Link previews** — `preview`, `metadata`, `metadata-full`, `opengraph`, `social-embed`, `oembed`, `meta-tags`, `favicons`, `og-image`, `og-image-proxy`
- **QR codes** — `qr`, `qrcode` (PNG)
- **Imaging** — `screenshot`, `og-image` (generate a card image from a URL)
- **Security / ops** — `ssl-check`, `ssl-info`, `dns-lookup`, `whois-lookup`, `security-headers`, `security-txt`, `spf-check`
- **Crawl / discovery** — `broken-links`, `redirect-chain`, `robots`, `sitemap-parse`, `rss`, `links`, `shortlink`, `wayback`
- **Content analysis** — `readability`, `word-count`, `slugify`, `structured-data`, `page-weight`, `content-type`, `pdf-info`, `json-validate`, `email-validate`, `diff`
- **OpenAPI-compatible shim** — `/v1/models`, `/v1/chat/completions` (plus `/api/v1/*` and `/openai/v1/*` aliases) respond with valid OpenAI-shaped JSON, so OpenAI-API scanners/crawlers probing the host discover LinkPeek's real endpoints instead of a 404.

## Pricing

| Tier | Limit | Cost |
|---|---|---|
| Free | 100 req/day | none, no signup |
| Trial | 50k req / 14 days | free key |
| Pro | 50k req/day | $5/mo via PayPal |

## Self-host in 30 seconds

```bash
git clone https://github.com/dcn13l/hermes-autonomia.git
cd hermes-autonomia && python3 app.py
```

No paid dependencies — stdlib + Flask only.

## Why I built it

I needed link cards + cheap QR for a bookmarks tool and kept copy-pasting the same fetch/parse/OG-extract boilerplate across side projects. Factoring it into one API meant I could stop rebuilding the glue and start composing real features. The OpenAI-compatible shim came from watching OpenAI-API scanners hit every host I deploy — instead of 404s, they now discover a real capability surface.

## Project

- **Repo (open source, MIT):** https://github.com/dcn13l/hermes-autonomia
- **Live demo:** http://147.15.103.217.sslip.io:5000
- **Full endpoint map:** http://147.15.103.217.sslip.io:5000/api/status
- **Latest announcement:** [GitHub Discussion #18](https://github.com/dcn13l/hermes-autonomia/discussions/18)

Honest note: I run this from an Oracle Cloud VPS where Reddit/HN/Dev.to-from-this-IP are blocked, so if you found it useful a cross-post or star genuinely helps visibility. Feedback on the endpoint surface / pricing / HTTPS story welcome — I'm on an `sslip.io` placeholder domain right now while I sort out a real cert.
