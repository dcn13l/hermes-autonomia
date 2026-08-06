# Hacker News — comment-sized promo (Show HN style)

**Status:** DRAFT ONLY. Cannot auto-post to HN — requires login + minimum karma + site anti-bot measures. The realistic path to HN is to **write it as a Show HN submission from your own account after building a small karma buffer via genuine comments**. DO NOT post this from a low-karma/zero-history account — Show HN from new accounts usually gets caught in the rate-limited "show" queue or flagged.

Suggested submission link: https://news.ycombinator.com/submit

---

**Title (Show HN):**

Show HN: LinkPeek — link preview API (title/description/og:image) + QR code, open source, 100 req/day free

**Text (comment-style intro in case you post a starter comment):**

Built this because every link-preview service I tried was either paid-only, capped at ~10 req/day on the free tier, or had quietly shut down months later.

Two endpoints:
- `GET /api/preview?url=…` → JSON with title, description, og:image, favicon, site_name
- `GET /api/qr?text=…` → PNG

Free tier is 100 req/day per IP, no signup. Pro is $5/mo for 50k/day. Self-hostable — Flask + BeautifulSoup, ~200 lines.

Live: http://147.15.103.217.sslip.io:5000
Source: https://github.com/dcn13l/hermes-autonomia

The genuinely hard part has been reliably extracting og:image across the wild variety of site markup out there. If you spot a URL it gets wrong, I'd want to hear about it: https://github.com/dcn13l/hermes-autonomia/issues

I'd specifically like feedback from anyone who's fallen back to "just self-host an OpenGraph library" — what would it take for you to prefer a hosted API (CORS proxy, handled rate-limiting, server-side scraper headers) over a library in your own process?

---

**Posting reality check for the autonomous agent:**
- HN has no public unauthenticated POST endpoint — posting requires a logged-in browser session + `--cookie`-style auth, and is業務 gated on account age + karma.
- Repeated identical Show HN posts from the same account within ~1 week create noise and attract downvotes/flags. Post this **once**, when there's supporting signal (a few trial-key activations, a Reddit thread with comments, an inbound GH star or two) so the Show HN actually has substance behind it.
- If the operator wants this posted, the cleanest path is: open https://news.ycombinator.com/login in their own browser, paste the title above, and submit. The agent should not impersonate a human HN account.
