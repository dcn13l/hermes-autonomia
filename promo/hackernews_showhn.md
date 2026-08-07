# Show HN submission draft + posting notes

## title

Show HN: I built a free link-preview API with 43 endpoints (QR codes, screenshots, email/MX, tech-stack)

## url

https://github.com/dcn13l/hermes-autonomia

## text (Show HN OP body)

Hi HN — I built LinkPeek, a free, self-hostable API that turns any URL or string into clean JSON metadata plus a few utility primitives. Forty-three endpoints, stdlib-only Python, no signup for the free tier (100 req/day per IP).

The pitch in one `curl`:

    curl http://147.15.103.217.sslip.io:5000/api/preview?url=news.ycombinator.com

returns title / description / og:image / favicon / site_name / quota object in one round trip.

Beyond link previews, the same server does QR codes, screenshots, OpenGraph, favicon fetch, redirect chains, SSL info, DNS lookup, tech-stack detection, robots.txt, sitemap parsing, broken-link check, oEmbed, RSS, structured-data, readability, word-count + reading time, PDF info, HTML diff, URL shortener, batch, email/MX validation, and a few ops endpoints (`/api/health`, `/api/status`, `/api/pricing`).

Why an API instead of a library:

- A bunch of sites block your server's IP but allow a Googlebot UA — handled server-side.
- OG markup is inconsistent: `og:image`, just Twitter Cards, or nothing → fall back to favicon + first `<h1>`.
- CORS — browser apps can't fetch `example.com/` cross-origin without a proxy. LinkPeek is the proxy.

Pricing: free 100 req/day, no key; free 14-day trial key via `GET /api/key?email=…`; Pro $5/mo via PayPal.me for 50k req/day, non-expiring key (`GET /api/subscribe?email=…` returns the key + payment link in the same response).

Repo: https://github.com/dcn13l/hermes-autonomia  
Live: http://147.15.103.217.sslip.io:5000

Source is one file of stdlib Python (no Flask/FastAPI), systemd unit included. Feedback welcome — what endpoints would be useful for you?

## posting notes (NOT part of the submission)

- HN posting requires a logged-in browser session + account. No HN creds in this environment.
- A zero-karma account can technically submit Show HN, but it usually gets throttled/flagged. Build ~1 week of genuine comment karma first on Show HN threads before submitting your own Show HN.
- Submit at https://news.ycombinator.com/submit (during weekday morning PST-ish for best HN traffic).
- Show HN convention: title starts with "Show HN:" and body text should avoid hype words and be honest about status. The draft above follows that.
- After posting, the comment-vote ratio matters. Engage genuinely; don't beg for upvotes.
- HN doesn't have an official posting API; you must use the web UI from a real browser/profile. Do NOT try to curl `https://news.ycombinator.com/submit` — they block bots aggressively and it can shadow-flag the account.
- Cross-reference: GitHub discussion announcing v1.8.6 (43 endpoints) → https://github.com/dcn13l/hermes-autonomia/discussions/10
- Reddit IP-block documented in repo README and https://github.com/dcn13l/hermes-autonomia/discussions/10