---
title: "I built a free API with 43 endpoints for link previews, QR codes, and web metadata"
published: false
description: "Drop in a URL, get back title/description/OG image/favicon/QR/screenshot/email-MX/tech-stack as JSON. 43 endpoints, free tier needs no signup, $5/mo Pro. Self-hostable stdlib Python."
tags: sideproject, api, python, opensource, linkpreview
canonical_url: https://github.com/dcn13l/hermes-autonomia/discussions/10
cover_image:
---

## The problem

Every chat bot, bookmark app, "share to X" flow, and link-in-bio page needs the same plumbing: parse OpenGraph tags, follow redirects, grab the favicon, render a screenshot, maybe validate an email or detect the site's tech stack. Most devs rewrite it from scratch, hit CORS walls, watch the OG-thumbnail startup they relied on sunset, and ship late.

I got bored of that, so I built **LinkPeek** — a single self-hostable API that returns link metadata, QR codes, screenshots, readability scores, email/MX validation, tech-stack detection, and a bunch more, in one `curl`. Forty-three endpoints. Free tier needs **no API key, no signup, no credit card**.

## Live examples (all tested 2026-08-07)

### 1. Link preview

```bash
curl http://147.15.103.217.sslip.io:5000/api/preview?url=github.com
```

```json
{
  "title": "Example Domain",
  "description": "",
  "favicon": "https://example.com/favicon.ico",
  "image": "",
  "site_name": "",
  "quota": { "limit": 100, "used_today": 2 }
}
```

### 2. QR code (PNG)

```bash
curl "http://147.15.103.217.sslip.io:5000/api/qr?text=https://example.com" --output qr.png
```

### 3. Email validation (RFC5322 + MX lookup)

```bash
curl "http://147.15.103.217.sslip.io:5000/api/email-validate?email=test@gmail.com"
```

```json
{
  "email": "test@gmail.com",
  "domain": "gmail.com",
  "has_mx": true,
  "mx_parsed": [
    { "host": "gmail-smtp-in.l.google.com", "priority": 5 }
  ]
}
```

### 4. Word count + reading time

```bash
curl "http://147.15.103.217.sslip.io:5000/api/word-count?url=example.com"
```

```json
{
  "title": "Example Domain",
  "char_count": 127,
  "char_count_no_spaces": 109,
  "reading_time_seconds": 6,
  "reading_wpm": 200,
  "sentence_count": 2,
  "avg_word_length": 5.63
}
```

### 5. Screenshot

```bash
curl "http://147.15.103.217.sslip.io:5000/api/screenshot?url=github.com" --output shot.png
```

### 6. Tech-stack detection

```bash
curl "http://147.15.103.217.sslip.io:5000/api/tech-stack?url=example.com"
```
Returns `server`, `x_powered_by`, detected `technologies[]`, `generator`.

### 7. Readability extract

```bash
curl "http://147.15.103.217.sslip.io:5000/api/readability?url=example.com"
```
Returns article `text`, `excerpt`, `headings[]`, `char_count`, `full_text_length`.

## All 43 endpoints

| Group | Endpoints |
|---|---|
| Link preview | `/api/preview`, `/api/extract`, `/api/metadata-full`, `/api/opengraph`, `/api/meta-tags` |
| QR codes | `/api/qr`, `/api/qrcode`, `/api/og-image`, `/api/og-image-proxy` |
| Screenshots | `/api/screenshot`, `/api/screenshot-url-hint` |
| Site metadata | `/api/favicons`, `/api/headers`, `/api/redirect-chain`, `/api/content-type`, `/api/ssl-info`, `/api/dns-lookup`, `/api/tech-stack` |
| Crawling | `/api/robots`, `/api/sitemap-parse`, `/api/broken-links`, `/api/oembed`, `/api/rss`, `/api/links`, `/api/structured-data` |
| Content | `/api/readability`, `/api/word-count`, `/api/pdf-info`, `/api/diff`, `/api/batch` |
| Utility | `/api/shortlink`, `/api/email-validate` |
| Ops/billing | `/api/health`, `/api/status`, `/api/stats`, `/api/pricing`, `/api/key`, `/api/subscribe`, `/api/validate-key`, `/api/donate`, `/api/webhook` |

Plus a legacy `/api/emaill-validate` alias. All are `GET` (apart from `/api/batch`, `/api/webhook`, `/api/donate`).

## Pricing (deliberately simple)

- **Free** — 100 req/day, no key, no signup. The quota object is in every response so you can show users how much they have left.
- **Trial** — free 14-day key at `GET /api/key?email=you@mail.com`.
- **Pro** — $5/mo via [PayPal.me/linkpeekpro](https://paypal.me/linkpeekpro). 50,000 req/day, non-expiring key. `GET /api/subscribe?email=…` returns your key and the payment link in the same response.

## Why an API instead of a library?

A library runs in your process. That's fine until you hit:

- **Sites that block your server's IP** but allow a generic Googlebot-style fetch — solved server-side with the right headers.
- **Inconsistent OG markup** — some sites bury `og:image`, some only have Twitter Cards, some have nothing and you fall back to favicon + first `<h1>`.
- **CORS** — browser apps can't reach `example.com/` cross-origin without a proxy. LinkPeek *is* the proxy.

A hosted API also decouples you from your backend: a static site or edge function can call it with no Node/Python runtime in the middle.

## Self-hostable

Single-file stdlib Python (no Flask/FastAPI dependency). Clone [`dcn13l/hermes-autonomia`](https://github.com/dcn13l/hermes-autonomia), run `python3 product/app.py`, done. systemd unit included.

## Honest distribution note

Reddit's edge security 403-blocks the VPS this runs on, so I can't post to r/SideProject / r/webdev / r/coolgithubprojects from here. If you have a Reddit account and the API is useful, a genuine cross-post would be hugely appreciated — mods are friendlier to posts that show the working `curl` examples rather than just a link.

Repo: [github.com/dcn13l/hermes-autonomia](https://github.com/dcn13l/hermes-autonomia)  
Discussion thread: [github.com/dcn13l/hermes-autonomia/discussions/10](https://github.com/dcn13l/hermes-autonomia/discussions/10)  
Live demo: `curl http://147.15.103.217.sslip.io:5000/api/preview?url=dev.to`

Feedback welcome.