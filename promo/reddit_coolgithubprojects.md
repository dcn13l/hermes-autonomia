<!-- POST TO: r/coolgithubprojects -->
<!-- URL: https://www.reddit.com/r/coolgithubprojects/submit -->
<!-- Cannot be posted by the agent: www.reddit.com returns HTTP 403 from this Oracle Cloud VPS (IP-blocked at the edge, re-confirmed this run). Paste from a normal browser session logged into YOUR Reddit account. -->

**Title:** [Show] LinkPeek — a self-hostable "swiss army knife" web API: 65 endpoints (link previews, QR codes, screenshots, DNS/SSL checks, Wayback lookup) in one stdlib Python file, no signup to use

**Body:**

Open-sourced a side project that turned into a 65-endpoint web-utility API. **stdlib Python + Flask, no paid deps, self-host in one `python3 app.py`.** Free 100 req/day with no signup.

**Try it now — no key, real output captured 2026-08-08:**

```bash
curl "http://147.15.103.217.sslip.io:5000/api/preview?url=github.com"
# -> {"title":"GitHub · Change is constant. GitHub keeps ...",
#     "description":"Join the world's most widely adopted, AI-powered ...",
#     "image":"https://images.ctfassets.net/.../GH-Homepage-Universe-img.png",
#     "favicon":"https://github.com/fluidicon.png","site_name":"GitHub",
#     "quota":{"limit":100,"used_today":1}}

curl -o qr.png "http://147.15.103.217.sslip.io:5000/api/qr?text=hello"   # 200 image/png
curl "http://147.15.103.217.sslip.io:5000/api/dns-lookup?url=github.com"
curl "http://147.15.103.217.sslip.io:5000/api/ssl-check?url=github.com"
curl "http://147.15.103.217.sslip.io:5000/api/wayback?url=github.com"   # Wayback Machine lookup
```

**65 endpoints — full list:** http://147.15.103.217.sslip.io:5000/api/status

Categories:
- **Link previews** — `preview`, `metadata`, `opengraph`, `social-embed`, `oembed`
- **QR codes** — `qr`, `qrcode` (PNG)
- **Imaging** — `screenshot`, `og-image`, `og-image-proxy`
- **Security/ops** — `ssl-check`, `ssl-info`, `dns-lookup`, `whois-lookup`, `security-headers`, `security-txt`, `spf-check`
- **Crawl/discovery** — `broken-links`, `redirect-chain`, `robots`, `sitemap-parse`, `rss`, `links`
- **Content** — `readability`, `word-count`, `slugify`, `structured-data`, `page-weight`, `content-type`, `pdf-info`
- **OpenAI-compatible** — `/v1/models` + `/v1/chat/completions` return valid OpenAI-shaped JSON so OpenAI-scanners discover real endpoints instead of 404

**Repo:** https://github.com/dcn13l/hermes-autonomia
**Live:** http://147.15.103.217.sslip.io:5000

The unusual/cool bit for me was the OpenAI-compatible shim: it makes the API self-describing to the growing fleet of OpenAI-API scanners/crawlers, turning every capability into a "model" the OpenAI shape already understands. Curious what r/coolgithubprojects thinks of that pattern — feel like it's either neat or a hack, would value honest takes.
