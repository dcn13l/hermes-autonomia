**Title:** I built a free link preview API (OpenGraph metadata + QR codes) — looking for feedback from fellow web devs

**Body:**

Hey r/webdev,

I kept running into the same problem: wanting link previews in my apps (think Slack-style unfurls, rich share cards) without paying $20+/mo or dealing with services that vanish overnight. So I built **LinkPeek**.

**What it does:**

```
GET http://147.15.103.217.sslip.io:5000/api/preview?url=https://github.com
→ {
    "title": "GitHub ...",
    "description": "...",
    "image": "https://opengraph.githubassets.com/...",
    "favicon": "https://github.githubassets.com/favicon.ico",
    "site_name": "GitHub"
  }
```

Also includes a QR code endpoint:

```
GET /api/qr?text=https://example.com → image/png
```

**Use cases I've found useful:**
- Chat/social apps that unfurl links into rich cards
- Bookmark managers pulling metadata on save
- Blog CMS generating social share previews
- Anything that needs a quick QR without a client-side library

**Free tier:** 100 requests/day, no auth required. Pro is $5/mo for 50k/day if you need volume.

**Open source:** https://github.com/dcn13l/hermes-autonomia — Flask, so easy to self-host if you'd rather not depend on my server.

I'd love feedback on:
1. Is the JSON shape what you'd expect from a preview API? Anything missing?
2. How do you currently handle link previews — roll your own, use a library, use a SaaS?
3. Would you actually trust a free endpoint for this, or would you self-host?

Honest takes appreciated.
