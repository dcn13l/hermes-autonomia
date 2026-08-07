# Product Hunt Launch Draft — LinkPeek

## Product Name
LinkPeek — Free Link Preview & QR Code API

## Tagline
Drop in a URL, get back OG metadata + QR codes as JSON. No signup, no auth.

## Description

**LinkPeek** is a free, open-source REST API for extracting link preview metadata and generating QR codes.

### What it does:
- **Link Preview:** Send any URL → get back `{title, description, og:image, favicon, site_name}` as clean JSON
- **QR Code Generator:** Send any text/url → get back a PNG QR code instantly
- **32+ endpoints:** preview, extract, batch (5 URLs), metadata-full, sitemap parsing, og-image proxy, URL shortener, health checks, and more
- **No signup, no auth** for the free tier — 100 requests/day per IP
- **Pro tier:** $5/mo for 50k requests/day

### Demo:
```bash
curl "http://147.15.103.217.sslip.io:5000/api/preview?url=https://github.com"
# → {"title":"GitHub · Change is constant...","description":"...","image":"..."}

curl "http://147.15.103.217.sslip.io:5000/api/qr?text=hello" -o qr.png
# → QR code PNG
```

### Pricing:
- **Free:** 100 req/day, no signup needed
- **Pro:** $5/month for 50,000 req/day

### Links:
- Repo: https://github.com/dcn13l/hermes-autonomia
- Live API: http://147.15.103.217.sslip.io:5000
- Discussion: https://github.com/dcn13l/hermes-autonomia/discussions/2

---

## Categories on Product Hunt
- Developer Tools
- APIs
- Open Source

## Audience
Developers building Discord/Telegram bots, link previews, social sharing, web scrapers, QR code app
