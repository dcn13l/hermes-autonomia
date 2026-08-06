---
title: "LinkPeek — Free Open-Source Link Preview API + QR Code API (100 req/day, no signup)"
published: false
description: "Drop in a URL, get back title/description/OG image/favicon as JSON. Plus a QR code endpoint. Self-hostable, $0 to start, $5/mo for 50k/day."
tags: sideproject, api, flask, opensource
canonical_url: https://github.com/dcn13l/hermes-autonomia
---

# LinkPeek — Free Open-Source Link Preview API + QR Code API

Every time I needed link previews — chat unfurls, share cards, bookmark metadata — I hit the same wall: the service was either **paid-only**, capped at something like **10 requests/day** on the free tier, or had **quietly shut down** months later (looking at you, half the OG-preview startups from 2019). So I built my own and open-sourced it.

## What it does

Two endpoints, no SDK, no auth for the free tier:

```bash
# Link preview — returns OpenGraph metadata as JSON
curl "http://147.15.103.217.sslip.io:5000/api/preview?url=https://github.com"
```

```json
{
  "title": "GitHub · Change is constant. GitHub keeps …",
  "description": "Join the world's most widely adopted, AI-powered developer platform…",
  "image": "https://images.ctfassets.net/8aevphvgewt8/…/GH-Homepage-Universe-img.png",
  "favicon": "https://github.com/fluidicon.png",
  "site_name": "GitHub",
  "quota": { "limit": 100, "used_today": 1 }
}
```

```bash
# QR code — returns a PNG
curl "http://147.15.103.217.sslip.io:5000/api/qr?text=https://example.com" --output qr.png
```

That's it. One request, one JSON object, one PNG. The whole point was to remove the ceremony around "I just need the title and image for this URL."

## Why use this over a library?

A library runs in your process. That's fine until you hit:
- **Sites that block your server's IP** but allow a generic Googlebot-style fetch — solved server-side with the right headers.
- **Inconsistent OG markup** — some sites bury `<meta property="og:image">`, some only have Twitter Cards, some have nothing and you fall back to the favicon + first `<h1>`.
- **CORS** — your browser app can't call `example.com/` cross-origin without a proxy. This *is* the proxy.

A hosted API also decouples you from your backend: a static site or an edge function can call it with no Node/Python runtime in the middle.

## Pricing (deliberately simple)

| Tier  | Daily limit     | Auth              | How to get                            | Price |
|-------|-----------------|-------------------|---------------------------------------|-------|
| Free  | 100 requests    | none (per-IP)     | just call `/api/preview?url=…`        | $0    |
| Trial | 50,000 requests | 14-day API key    | `GET /api/key?email=you@mail.com`     | $0    |
| Pro   | 50,000 requests | permanent API key | `GET /api/subscribe?email=you@mail.com` → returns key + payment link | $5/mo |

No signup wall on the free tier — just call the endpoint and you're rate-limited per-IP at 100/day. The quota object is in every response so you can show your users how much they have left, or flip yourself to a paid key without code changes.

## Self-hostable

The full app is in [the repo](https://github.com/dcn13l/hermes-autonomia). It's a Flask app — `flask run` and you're done:

```bash
git clone https://github.com/dcn13l/hermes-autonomia
cd hermes-autonomia
pip install -r requirements.txt
flask run
```

Pinned versions live in `requirements.txt`. The whole thing is small enough to read in one sitting — `app.py` is the endpoint logic, `decorators.py` is the rate-limit/Pro-key gate, `keys.json` is the customer record (back it up).

## What I'd love feedback on

- **OG image extraction reliability** — the genuinely fiddly part. Some sites serve the image over a CDN that rotates the URL, some block scrapers, some just don't have an `og:image` at all. I'd like to know which URLs it fails on.
- **Would you use a hosted API, or is "just self-host an OpenGraph library" good enough for your use case?** Be honest — that's valid.
- **Feature gaps**: batch requests? PDF cover previews? oEmbed? Returning the Twitter Card as a fallback when OG is missing? Tell me which one would tip you from "interesting" to "I'd actually integrate this."

## Stack

Flask · BeautifulSoup for HTML parsing · Flask-Limiter for rate limiting · Werkzeug for the dev server (systemd + nginx in prod). No database — `keys.json` is the customer record, which keeps the operational surface tiny for a hobby-scale API that I'd like to grow into something real.

---

**Live demo:** http://147.15.103.217.sslip.io:5000
**Source:** https://github.com/dcn13l/hermes-autonomia

I'm a solo dev, not a company. The free tier actually free — if 100/day is too low for your prototype, grab a 14-day trial key with `/api/key?email=…` and stretch it to 50k while you test. If you ship it to production, $5/mo. That's the whole pitch.

Questions / hate / feature requests: reply or open an issue. 🙏
