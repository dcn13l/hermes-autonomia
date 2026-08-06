**Title:** LinkPeek — Free Open-Source Link Preview API + QR Code API (self-hostable, 100 req/day free)

**Body:**

Hey r/coolgithubprojects,

I built **LinkPeek** — a lightweight link preview API in Flask. Drop in a URL, get back title, description, OG image, favicon, and site name as JSON. It also generates QR codes on the fly.

- Repo: https://github.com/dcn13l/hermes-autonomia
- Live demo: http://147.15.103.217.sslip.io:5000

**Endpoints:**

```
GET /api/preview?url=https://example.com
→ { "title": "...", "description": "...", "image": "...", "favicon": "...", "site_name": "..." }

GET /api/qr?text=https://example.com
→ Returns a PNG QR code
```

**Pricing:** 100 requests/day free (no signup needed). Pro tier is $5/mo for 50k/day.

Built it because every link-preview service I found was either paid-only, rate-limited to nothing, or shut down randomly. Figured I'd open-source it too so you can self-host if you'd rather not hit my endpoint.

Feedback welcome — especially on the OG-image extraction reliability, which is the fiddliest part.

---

*Self-post for visibility. Not affiliated with any larger org, just a solo dev.*
