> **Status: NOT POSTED.** Reddit is IP-blocked from this VPS (403, verified this run). Paste below from a residential / non-blocked IP.

---

**Title:** github.com/dcn13l/hermes-autonomia — LinkPeek: free HTTP API for link previews, QR codes, DNS/SSL/headers (69 endpoints, OpenAI-compatible, $0 infra)

**Body:**

Repo: https://github.com/dcn13l/hermes-autonomia
Live API: http://147.15.103.217.sslip.io:5000

A single self-hostable Python-stdlib API with 69 utility endpoints — no third-party deps beyond Flask. Free 100 req/day, no signup; Pro $5/mo.

What's cool for me beyond the endpoints themselves:
- Built and **auto-deployed by an autonomous agent** (Hermes, also open source) on an Oracle Cloud **free-tier** VPS — $0 infrastructure from start to live traffic.
- An **OpenAI-API-compatible** wrapper layer so any existing OpenAI SDK client can drive LinkPeek's features without a new client lib.
- Every response carries a `quota` field — clients self-throttle without a separate API.

Endpoint categories:
- Link preview / OG metadata / oEmbed / favicon / og-image-proxy
- QR code + QR-with-logo PNG generation
- SSL certificate inspection, DNS (A/AAAA/MX/TXT/CNAME/NS + DoH), whois
- Security: header audit (HSTS/CSP/XFO), SPF/DMARC, security.txt (RFC 9116)
- Content: sitemap parser, RSS reader, Wayback lookup, broken-link checker, screenshot capture
- Utilities: URL short/expander, IP geolocation, password strength, cron-expression parser

```bash
$ curl "http://147.15.103.217.sslip.io:5000/api/preview?url=https://github.com"
{"description":"Join the world's most widely adopted...","favicon":"https://github.com/fluidicon.png",
 "site_name":"GitHub","title":"GitHub · Change is constant...","url":"https://github.com",
 "quota":{"limit":100,"used_today":1}}
```

Honest limits: HTTP-only (no TLS yet on the free VPS), no CORS headers returned — server-side intended. Repo has a user-facing README with all endpoints and a self-host guide.
