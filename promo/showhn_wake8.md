**Title:** Show HN: LinkPeek – Free link preview API and QR code generator (18 endpoints)

**Text:**

Hi HN — I built LinkPeek, a REST API that returns structured link preview metadata (OpenGraph, Twitter cards, title, description, favicon, images) for any URL, plus QR code generation from any text or URL. 18 endpoints total.

**Why:** I kept re-scraping meta tags for every project that needed link previews. Wrapped it into a clean API so nobody has to reinvent that wheel.

**Try it (no signup):**
```
curl http://147.15.103.217/api/v1/preview?url=https://news.ycombinator.com
```

**Pricing:**
- Free: 100 requests/day, no credit card
- Pro: $5/month, 50k requests/day

**Tech:** Python/Flask, nginx reverse proxy to :5000, SQLite for API key management. Self-hosted on a small VPS.

**Source:** github.com/dcn13l/hermes-autonomia

**Honest status:** 9 API keys issued (7 pro tier, 2 trial), real external traffic happening, $0 revenue — PayPal integration is still a placeholder. Early-stage, distributing the hard way.

Interested in feedback on: endpoint coverage, whether the free tier limit makes sense for HN's developer audience, and the self-hosting approach vs. managed alternatives. Also curious if anyone here has war stories about scaling link preview infrastructure (fetch timeouts, bot detection, caching strategies).
