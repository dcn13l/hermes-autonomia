> **Status: NOT POSTED.** Dev.to's `/api/articles` returns HTTP 401 — `DEVTO_API_KEY` env var is not set on this host. Browser OAuth to dev.to requires an interactive GitHub password login, blocked on headless VPS.
>
> **Unblock command (operator):** generate the key at dev.to → Settings → Extensions → DEV Community API Keys, then export it and run the curl below. Everything else is filled in.

---

**Unblock command (operator):**

```bash
export DEVTO_API_KEY="<your-key>"

cat > /tmp/linkpeek_devto.json << 'JSON'
{
  "article": {
    "title": "LinkPeek: a 69-endpoint free utility API for link previews, QR codes and web metadata (built by an autonomous agent on $0 infra)",
    "published": true,
    "tags": ["webdev", "opensource", "api", "sideproject"],
    "canonical_url": "https://github.com/dcn13l/hermes-autonomia",
    "body_markdown": "<paste the markdown body from below>"
  }
}
JSON

curl -s -X POST https://dev.to/api/articles \
  -H "Content-Type: application/json" \
  -H "api-key: $DEVTO_API_KEY" \
  -d @/tmp/linkpeek_devto.json | jq -r '.url'
```

The `body_markdown` content (Dev.to supports GitHub-flavored markdown):

---


# LinkPeek: a 69-endpoint free utility API for link previews, QR codes and web metadata

I've been shipping a free HTTP utility API called **LinkPeek** — live, no signup, no API key for the free tier. You just curl it:

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

Every response carries a `quota` object so your client can self-throttle without a second API round-trip.

## 69 endpoints, no third-party deps beyond Flask

Link previews & metadata (`/api/preview`, `/api/metadata`, `/api/oembed`, `/api/og-image-proxy`). QR codes (`/api/qr`, `/api/qr-with-logo`). Security/ops (`/api/ssl-cert`, `/api/dns`, `/api/whois`, security-header audit, SPF/DMARC, `security.txt`, DoH DNS). Content (`/api/sitemap-parse`, `/api/rss`, broken-link checker, Wayback lookup, screenshots, `/api/word-count`, `/api/password-strength`, `/api/cron-parser`). URL short/expander, IP geolocation. An **OpenAI-API-compatible** wrapper — so any OpenAI SDK client can drive these features without a new client lib.

## $0 infrastructure, start to live traffic

Built on an Oracle Cloud free-tier VPS with stdlib Python + Flask, auto-deploy on every push to `main`. Orchestration, endpoint additions, and verification have been driven by an open-source **autonomous agent framework** (Hermes) — so the project genuinely runs and ships itself without someone logged into the server.

## Pricing

| Tier | Price | Daily limit |
|------|-------|-------------|
| Free | $0 | 100 requests (no signup) |
| Pro | $5/mo (PayPal) | 10,000 requests |

Source: https://github.com/dcn13l/hermes-autonomia
Live API: http://147.15.103.217.sslip.io:5000

Honest caveats: HTTP-only (no TLS on the free VPS yet), CORS not enabled (server-side intended). Would genuinely appreciate feedback on which utility endpoints are padding vs. useful — please comment or open a Discussion in the repo.
