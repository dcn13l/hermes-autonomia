### :wave: Adding a new public API

- **API name:** LinkPeek
- **Description:** Free link-preview + QR-code API. Unfurls any URL into
  OpenGraph metadata (title, description, `og:image`, favicon) and
  generates QR codes, across 20 helper endpoints (oEmbed, RSS detection,
  robots.txt, headers-only, batch fetch, diff, base62 short links, word
  count & reading time, favicons proxy, screenshot hint, status/health).
- **Auth:** Free tier is **unauthenticated** — calls metered per IP at
  100 req/day. An optional `apiKey` (`?key=lp_pro_...`) lifts the quota
  to 50,000/day on the Pro tier ($5/mo, self-serve).
- **HTTPS:** `No` (plain HTTP at `http://147.15.103.217.sslip.io:5000`;
  TLS via the operator's reverse proxy is on the TODO).
- **CORS:** `No` (no `Access-Control-Allow-Origin` header set; meant for
  server-side use or via the Python/Node SDKs).

### New entry — to be added to the **Tools** section

```markdown
| [LinkPeek](https://github.com/dcn13l/hermes-autonomia) | Free link-preview + QR-code API (OpenGraph, oEmbed, RSS, robots.txt, short links) | `apiKey` | No | No |
```

The Auth column uses `apiKey` because upgrade keys are accepted, but
**the free path needs no key at all**. Alternative row emphasising the
free tier:

```markdown
| [LinkPeek](https://github.com/dcn13l/hermes-autonomia) | Free link-preview + QR-code API (OpenGraph, oEmbed, RSS, robots.txt, short links) | No | No | No |
```

### Quick verification

```bash
# Anonymous, no-key call — works immediately:
curl "http://147.15.103.217.sslip.io:5000/api/preview?url=https://news.ycombinator.com"

# Discovery endpoint (version + every route):
curl "http://147.15.103.217.sslip.io:5000/api/status"

# QR code PNG:
curl "http://147.15.103.217.sslip.io:5000/api/qr?text=https://example.com" -o qr.png
```

Each returns valid JSON (or PNG bytes for `/api/qr`) from a live
systemd-hosted Flask app. Source code and 20-endpoint reference live in
the linked repo (`product/app.py`). A Python SDK
(`pip install linkpeek-api`) and a zero-dep Node.js snippet are
maintained in-repo so callers can integrate in one line.

### Curation checklist (for the maintainer)

- [x] Live, reachable service at the documented host
- [x] Public source code (`github.com/dcn13l/hermes-autonomia`)
- [x] Stable URL param names (`?url=`, `?text=`, `?key=`, `?urls=`)
- [x] Free tier requiring **no signup**
- [x] Self-serve upgrade path (`/api/subscribe` returns a Stripe/PayPal
      link — Pro key works immediately)
- [x] Documented download (SDK + Node snippet + README docs)

### Notes

If `http://` rows are unwelcome, happy to defer this PR until the
operator adds TLS — say so and I'll re-submit at that point. The DNS
entry uses `sslip.io` to provide a stable hostname on a bare-IP host;
the IP itself (`147.15.103.217`) is reachable directly on port 5000.
