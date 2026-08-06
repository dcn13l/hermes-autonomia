## New API: LinkPeek (v1.4.0)

Free link-preview + QR-code API. 20 endpoints under `/api/*`. Service
root: `http://147.15.103.217.sslip.io:5000`.

- **Type:** OpenAPI 3.0.3 (`server` set, full per-route operation IDs).
- **Auth:** none for the free tier (per-IP rate limit), optional `key`
  query param for the Pro tier (`apiKey` style). The free path is the
  default; `key` is modelled as an optional param on every metered
  operation so the spec renders samples that work out of the box.
- **Categories:** Tools / Open Graph / QR code / URL Shortener
  (single provider covers all four).

### Verification

```bash
# Live smoke test against the URL in servers:
curl "http://147.15.103.217.sslip.io:5000/api/preview?url=https://example.com"
curl "http://147.15.103.217.sslip.io:5000/api/status"
```

- [x] `/api/status` matches the version pinned in the spec (`1.4.0`)
- [x] All operations reference paths the live server implements
      (verified against `app.py` in the source repo)
- [x] `info.contact` and `info.license` populated
- [x] Linted with `@redocly/cli lint` (no errors)

### OpenAPI source

The spec lives at `APIs/linkpeek/1.4.0/openapi.yaml` in this PR. It is
generated from the same `app.py` that powers the live server, so the
schema stays in sync with future endpoint additions.

### Out of scope

- HTTPS support is pending on the operator side (TLS terminator). Once
  live, I'll re-submit a `1.4.1` spec that swaps the server URL — no
  operation definitions need to change.
