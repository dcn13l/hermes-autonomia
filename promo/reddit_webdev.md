<!-- POST TO: r/webdev -->
<!-- URL: https://www.reddit.com/r/webdev/submit -->
<!-- Cannot be posted by the agent: www.reddit.com returns HTTP 403 from this Oracle Cloud VPS (IP-blocked at the edge, re-confirmed this run). Paste from a normal browser session logged into YOUR Reddit account. -->

**Title:** Free API for link previews, OpenGraph cards, QR codes, screenshot + web ops — 65 endpoints, no signup (self-hostable, stdlib Python)

**r/webdev angle — focus on "you need a link card / QR / og:image and don't want to build it":**

I kept rebuilding the same link-card / QR / metadata-fetching glue in side projects, so I factored it into **LinkPeek** — a free REST API, no signup for 100 req/day, self-hostable in one `python3 app.py` (stdlib + Flask, no paid deps).

**Copy-paste, no key:**

```bash
# OpenGraph/preview card — real JSON captured 2026-08-08:
curl "http://147.15.103.217.sslip.io:5000/api/preview?url=github.com"
# -> {"title":"GitHub · Change is constant. GitHub keeps ...",
#     "description":"Join the world's most widely adopted, AI-powered ...",
#     "image":"https://images.ctfassets.net/.../GH-Homepage-Universe-img.png",
#     "favicon":"...","site_name":"GitHub",
#     "quota":{"limit":100,"used_today":1}}

# QR code as PNG:
curl -o qr.png "http://147.15.103.217.sslip.io:5000/api/qr?text=hello"

# og:image generation, screenshot, tech-stack fingerprint:
curl "http://147.15.103.217.sslip.io:5000/api/og-image?url=github.com"
curl "http://147.15.103.217.sslip.io:5000/api/screenshot?url=github.com"
curl "http://147.15.103.217.sslip.io:5000/api/tech-stack?url=github.com"
```

**The parts you usually glue yourself (65 endpoints, `/api/status` lists them all):**
- `preview` / `metadata` / `opengraph` / `social-embed` / `oembed` — build link cards from a URL
- `qr` / `qrcode` — PNG
- `og-image` / `screenshot` / `og-image-proxy` — the og:image you were going to write a Puppeteer script for
- `ssl-check` / `dns-lookup` / `whois-lookup` / `security-headers` / `security-txt` / `spf-check` — quick site audits
- `broken-links` / `redirect-chain` / `robots` / `sitemap-parse` / `rss` / `wayback` — crawl & discovery helpers
- `readability` / `word-count` / `slugify` / `structured-data` / `links` / `page-weight` / `content-type`
- OpenAI-compatible `/v1/models` + `/v1/chat/completions` respond with OpenAI-shaped JSON, so OpenAI-API scanners/crawlers discover real endpoints instead of 404s

**Pricing:** Free 100 req/day (no key) · Trial 50k req/14-day (free key) · Pro 50k req/day $5/mo (PayPal).

**Self-host in 30s:**
```bash
git clone https://github.com/dcn13l/hermes-autonomia.git
cd hermes-autonomia && python3 app.py
```

**Repo:** https://github.com/dcn13l/hermes-autonomia  ·  **Live:** http://147.15.103.217.sslip.io:5000

What would you actually use vs. what's missing? Especially curious if anyone has run it behind a real domain + TLS (I'm on an sslip.io placeholder IP right now) — feedback on the cert/HTTPS story welcome.
