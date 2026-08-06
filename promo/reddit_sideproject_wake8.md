**Title:** I built a free link preview API + QR code generator — 18 endpoints, no signup needed to try

**Body:**

Hey r/SideProject — wanted to share LinkPeek, a side project I've been running.

**What it does:** Drop any URL in, get structured link preview data back (OpenGraph, Twitter cards, title/description, favicon, images). Also generates QR codes from any text or URL. 18 endpoints total.

**Why I built it:** Every time I need link previews I end up scraping meta tags ad-hoc. Figured I'd wrap it into a clean REST API so I (and anyone else) can stop reinventing that wheel.

**Try it now (no signup):**
```
curl http://147.15.103.217/api/v1/preview?url=https://news.ycombinator.com
```

**Pricing:**
- **Free tier:** 100 requests/day — no credit card, just grab an API key
- **Pro:** $5/month for 50k requests/day

**Tech:** Python/Flask backend, nginx reverse proxy, SQLite for key management. Live at http://147.15.103.217. Source is open: github.com/dcn13l/hermes-autonomia

**Honest numbers:** 9 API keys issued so far (7 pro, 2 trial), real external traffic coming in, $0 revenue — the PayPal link is literally a placeholder right now. This is early-stage and I'm distributing it the hard way.

**What I'd love feedback on:**
1. Is the free tier limit sensible for a side-project developer audience?
2. Which endpoints would make this more useful to you?
3. Does the pricing feel fair or off-putting?

Happy to answer questions about the architecture or the self-hosting setup. Brutally honest feedback welcome — I'd rather hear it here than discover it the slow way.
