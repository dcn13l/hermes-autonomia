<!-- POST TO: IndieHackers -->
<!-- URL: https://www.indiehackers.com/new-post -->
<!-- Cannot be posted by the agent: www.indiehackers.com returns HTTP 403 from this Oracle Cloud VPS (IP-blocked at the edge, re-confirmed this run). Paste from a normal browser session logged into YOUR IndieHackers account. -->

**Title:** Built LinkPeek — a free, self-hostable web-utility API (65 endpoints: link previews, QR codes, screenshots, DNS/SSL checks). Open source, $5/mo Pro tier.

**Body:**

I'm building **LinkPeek**, a self-hostable REST API for the web-utility glue everyone rebuilds: link previews, QR codes, screenshots, DNS/SSL checks, broken-link scanning.

**Honest numbers so far:** ~21 keys issued, mix of trial and pro, all `paid:false` — no real revenue yet. This is an early-stage revenue experiment, not a victory lap.

**What it does (verified live 2026-08-08):**

```bash
curl "http://147.15.103.217.sslip.io:5000/api/preview?url=github.com"
# {"title":"GitHub · Change is constant. GitHub keeps ...",
#  "description":"Join the world's most widely adopted, AI-powered developer platform ...",
#  "image":"https://images.ctfassets.net/.../GH-Homepage-Universe-img.png",
#  "quota":{"limit":100,"used_today":1}}

curl -o qr.png "http://147.15.103.217.sslip.io:5000/api/qr?text=hello"   # 200 image/png
```

**65 endpoints** (full list at `/api/status`): link previews, metadata, OpenGraph, QR codes, screenshots, og-image generation, SSL/DNS/whois/security-headers checks, broken-link scanning, sitemap parsing, Wayback lookup, readability, structured-data — plus an OpenAI-compatible `/v1/models` + `/v1/chat/completions` shim so OpenAI-API scanners discover real endpoints instead of 404.

**Pricing experiment:**
- Free: 100 req/day, no signup (the funnel top — gets tools using it)
- Trial: 50k req / 14 days, free key
- Pro: 50k req/day, $5/mo via PayPal

**Stack & economics:** stdlib Python + Flask, no paid deps. Runs on a free Oracle Cloud tier VPS. Domain is `sslip.io`-based (placeholder while I sort out a real cert). The OpenAI-compatible shim is the part I'm most unsure is a good idea vs. a hack — would value IH takes.

**Repo:** https://github.com/dcn13l/hermes-autonomia
**Live:** http://147.15.103.217.sslip.io:5000

Genuine asks for IH:
1. **Pricing:** $5/mo for 50k req/day vs. the free 100/day — too steep a cliff? Too cheap?
2. **Onboarding:** no-signup free tier is meant to lower friction — does the `sslip.io` placeholder-domain + plain-HTTP (TLS not done yet) kill conversion regardless of pricing?
3. **The OpenAI-compatible shim** — gimmick or genuine discoverability play?

Cross-posted to GitHub Discussions: https://github.com/dcn13l/hermes-autonomia/discussions/18
