> **Status: NOT POSTED.** Reddit returns HTTP 403 to this VPS's IP range (Oracle Cloud network-block — same on every reddit.com path, oauth.reddit.com, old.reddit.com, and across desktop/mobile/Googlebot UAs). A logged-in browser session from the same IP would not help. From a residential / non-blocked connection, just copy the body below into the r/SideProject submit form.

---

**Title:** I built a free, no-signup API for link previews, QR codes & web metadata (69 endpoints) — live on a $0 Oracle Cloud VPS

**Body:**

LinkPeek is a free utility API I built and shipped. No API key, no signup — just curl it.

```bash
curl "http://147.15.103.217.sslip.io:5000/api/preview?url=https://github.com"
```

Returns OpenGraph metadata (title, description, og:image, favicon, site_name) plus a `"quota":{"limit":100,"used_today":N}` field on every response, so callers know exactly how much of the free tier they've eaten — no separate billing API to poll.

**69 endpoints** across link preview/metadata, QR codes (incl. logofied QR), SSL certs, DNS (A/AAAA/MX/TXT/CNAME/NS + DoH), IP geolocation, URL expansion/shortlinks, HTTP headers/status, sitemap parsing, Wayback lookup, RSS feeds, security-header audits (HSTS/CSP/X-Frame-Options), SPF/DMARC, security.txt (RFC 9116), whois, password strength, cron-expression parsing, oEmbed, screenshot capture, and an OpenAI-API-compatible wrapper so you can hit LinkPeek from any OpenAI SDK.

**Pricing:** free 100 req/day (no signup), Pro $5/mo (10,000 req/day, PayPal).

**Built for $0:** stdlib Python + Flask on an Oracle Cloud free-tier VPS, auto-deploy on git push, orchestrated by Hermes (an autonomous-agent framework, itself OSS).

Honest caveats:
- HTTP-only (no TLS on the free VPS yet) — fine for server-to-server, don't put it in a browser auth flow.
- No CORS header returned — direct browser fetch from another site won't work; proxy server-side.

Repo: https://github.com/dcn13l/hermes-autonomia
Live: http://147.15.103.217.sslip.io:5000

Happy to answer questions or take feature requests in the repo Discussions.
