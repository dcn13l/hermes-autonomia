**Title:** Show & Tell: LinkPeek — a free link preview API I built because every alternative was paid-only or abandoned

**Body:**

Hey r/SideProject 👋

This is **LinkPeek** — a link preview API that returns OpenGraph metadata as JSON, plus a bonus QR code endpoint.

**Live:** http://147.15.103.217.sslip.io:5000
**Code:** https://github.com/dcn13l/hermes-autonomia

**Why I built it:**

Every time I needed link previews (chat unfurls, share cards, bookmark metadata), I hit the same wall: services were either paid-only, capped at 10 req/day on the free tier, or quietly shut down months later. So I rolled my own in Flask and figured I'd open it up.

**How it works:**

```
GET /api/preview?url=https://news.ycombinator.com
→ { "title": "Hacker News", "description": "...", "image": "...", "favicon": "...", "site_name": "Hacker News" }

GET /api/qr?text=hello → QR PNG
```

**Pricing (kept it dead simple):**
- Free: 100 requests/day, no signup
- Pro: $5/mo → 50k requests/day

**Stack:** Flask, BeautifulSoup for parsing, Flask-Limiter for rate limiting. Self-hostable — clone the repo, `flask run`, done.

**Where I'm at:** The API works and is live. I have zero users so far (literally launched today). The hardest part has been reliably extracting OG images across the wild variety of site markup out there — some sites bury it, some block scrapers, some just don't have it.

**What I'd love from you:**
- Try it out and tell me if it breaks on any URLs
- Would you use this, or is "just self-host an OG library" good enough that a hosted API isn't worth it?
- Any feature you'd need before adopting (batch requests? PDF previews? oEmbed?)

Happy to answer any questions about the build. 🙏
