> **Status: NOT POSTED.** Reddit is IP-blocked from this VPS (403 on every reddit.com path, verified this run). Paste the body below into the r/webdev submit form from a residential / non-blocked IP.

---

**Title:** LinkPeek — free link-preview / OG-metadata / QR / security-header API (69 endpoints, no signup, OpenAI-compatible)

**Body:**

For anyone building link cards, share dialogs, or link previews: I open-sourced a free utility API that returns all the metadata you'd normally scrape by hand — title, description, og:image, favicon, site_name, canonical URL. No API key for the free tier, just GET it:

```bash
curl "http://147.15.103.217.sslip.io:5000/api/preview?url=https://github.com"
```

```json
{
  "description": "Join the world's most widely adopted, AI-powered developer platform...",
  "favicon": "https://github.com/fluidicon.png",
  "image": "https://images.ctfassets.net/8aevphvgewt8/.../GH-Homepage-Universe-img.png",
  "site_name": "GitHub",
  "title": "GitHub · Change is constant. GitHub keeps you ahead. · GitHub",
  "url": "https://github.com",
  "quota": { "limit": 100, "used_today": 1 }
}
```

Why this exists: most "link preview" SaaS wants signup + a card before you can test anything. This one is live, HTTP-only, zero-config. The JSON carries a per-response `quota` object so frontend code can show "you have 99/100 left today" without a second round-trip.

69 endpoints, including:
- preview / metadata / oEmbed / og-image-proxy
- QR codes + QR-with-logo
- SSL cert inspection, DNS (A/AAAA/MX/TXT/CNAME/NS + DoH), whois
- Security headers (HSTS/CSP/X-Frame-Options), SPF/DMARC, security.txt
- Sitemap parser, RSS reader, Wayback lookup, broken-link checker
- URL shortener + expander, IP geolocation, password strength, cron parser
- An OpenAI-API-compatible endpoint so you can call any LinkPeek feature from an existing OpenAI SDK

**Free:** 100 req/day, no signup. **Pro:** $5/mo, 10,000 req/day.

Built for $0 on an Oracle Cloud free-tier VPS, stdlib Python + Flask, auto-deploy on push. Source + full README: https://github.com/dcn13l/hermes-autonomia. Live: http://147.15.103.217.sslip.io:5000.

Known limits to flag before you reach for it: HTTP-only (no TLS), no CORS header returned — so adapt it server-side rather than direct fetch from a browser page. Happy to add CORS / TLS if there's uptake.
