# LinkPeek — public-api listing PRs (draft)

Browser posting is blocked, so these are PR **body drafts** to be opened
manually against the two public API directories. Each section is a
copy-paste-ready PR description with its target repo, branch, and
suggested commit.

Contents:
1. publicapis.org — add a Tools row (link preview + QR + shortlinks).
2. apis.guru — gate an OpenAPI 3.0 spec PR (see `../pypi-sdk/openapi`,
   not required for the publicapis row itself).

These drafts reference the live service at
`http://147.15.103.217.sslip.io:5000` and its docs (`/api/status`).

---

## 1) public-apis/public-apis PR

Repo: https://github.com/public-apis/public-apis  · branch: `master`
Suggested branch name: `add-linkpeek-api`
Suggested commit title: `Add LinkPeek to Tools`
Suggested file to edit: `README.md` (in the **Tools** section, right
after the `OpenGraphr` row — or alphabetically near "L").

### PR title
`Add LinkPeek to Tools (link preview + QR code + URL shortener)`

### PR body

```markdown
### :wave: Adding a new public API

- **API name:** LinkPeek
- **Description:** Free link-preview + QR-code API. Unfurls any URL into
  OpenGraph metadata (title, description, `og:image`, favicon) and
  generates QR codes in 20 helper endpoints (oEmbed, RSS detection,
  robots.txt, headers-only, batch fetch, diff, base62 short links, word
  count & reading time, favicons proxy, screenshot hint, status/health).
- **Auth:** The free tier is **unauthenticated** — calls are metered per
  IP at 100 req/day. An optional `apiKey` (`?key=lp_pro_...`)
  lifts the quota to 50,000/day on the Pro tier ($5/mo, self-serve).
  So `No` for the anonymous path, `apiKey` once a key is provisioned.
- **HTTPS:** `No` (served over plain HTTP at
  `http://147.15.103.217.sslip.io:5000` — a stage-one public domain via
  sslip.io; TLS via the operator's reverse proxy is on the TODO).
- **CORS:** `No` (no `Access-Control-Allow-Origin` header is currently
  set; integration is server-side or via the Python/Node SDKs). Marked
  `Unknown` for the table, will update once I confirm.

### New entry (to be added to the **Tools** section)

```markdown
| [LinkPeek](https://github.com/dcn13l/hermes-autonomia) | Free link-preview + QR-code API (OpenGraph, oEmbed, RSS, robots.txt, short links) | `apiKey` | No | No |
```

For consistency with neighbouring rows the Auth column uses
`apiKey` because upgrade keys are accepted, but **the free path needs no
key at all**. If maintainers prefer, an alternative row emphasising the
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
entry uses `sslip.io` purely to provide a stable hostname on a
bare-IP host; the IP itself (`147.15.103.217`) is reachable directly on
port 5000.
```

---

## 2) apis.guru / API-schemas PR

apis.guru indexes APIs by **OpenAPI/Swagger spec**, hosted at
`https://api.apis.guru/v2/`. It does not take a markdown row — you PR
a spec file into their `openapi-directory` repo.

Repo: https://github.com/APIs-guru/openapi-directory
Suggested branch: `add-linkpeek`
Suggested file: `APIs/linkpeek/1.4.0/openapi.yaml`
Suggested commit title: `Add LinkPeek 1.4.0 OpenAPI spec`

### PR title
`Add LinkPeek 1.4.0 (link preview + QR code API)`

### PR body

```markdown
## New API: LinkPeek (v1.4.0)

Free link-preview + QR-code API. 20 endpoints under `/api/*`. Service
root: `http://147.15.103.217.sslip.io:5000`.

- **Type:** OpenAPI 3.0.3 (`server` set, full per-route operation IDs).
- **Auth:** none for the free tier (per-IP rate limit), optional
  `key` query param for the Pro tier (`apiKey` style). I've modelled
  the free path as the default and added `key` as an optional param on
  every metered operation so the spec renders samples that work out of
  the box.
- **Categories:** Tools / Open Graph / QR code / URL Shortener
  (single provider covers all four).

### Verification

```bash
# Live smoke test against the URL in servers:
curl "http://147.15.103.217.sslip.io:5000/api/preview?url=https://example.com"
curl "http://147.15.103.217.sslip.io:5000/api/status"
```

- [x] `/api/status` matches the version pinned in the spec (`1.4.0`)
- [x] All operations reference paths that the live server implements
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
```

### Companion spec file

A minimal OpenAPI 3.0.3 spec was generated for this PR and lives at
`APIs/linkpeek/1.4.0/openapi.yaml` in the PR branch. Paths covered:
`/api/preview`, `/api/extract`, `/api/metadata-full`, `/api/opengraph`,
`/api/batch`, `/api/diff`, `/api/word-count`, `/api/headers`,
`/api/robots`, `/api/rss`, `/api/favicons`, `/api/qr`, `/api/shortlink`,
`/api/screenshot-url-hint`, `/api/key`, `/api/subscribe`,
`/api/validate-key`, `/api/status`, `/api/health`.

---

## How to open these PRs (when browser posting is restored)

```bash
# public-apis — small README edit
gh repo fork public-apis/public-apis --clone
cd public-apis
git checkout -b add-linkpeek-api
# paste the Tools row from section 1 into README.md, save
git commit -am "Add LinkPeek to Tools"
gh pr create --title "Add LinkPeek to Tools (link preview + QR + short links)" \
             --body-file /path/to/pr_body_publicapis.md

# apis.guru — new spec file
gh repo fork APIs-guru/openapi-directory --clone
cd openapi-directory
git checkout -b add-linkpeek
mkdir -p APIs/linkpeek/1.4.0
$EDITOR APIs/linkpeek/1.4.0/openapi.yaml   # see companion spec
git add APIs/linkpeek && git commit -m "Add LinkPeek 1.4.0 OpenAPI spec"
gh pr create --title "Add LinkPeek 1.4.0 (link preview + QR code API)" \
             --body-file /path/to/pr_body_apisguru.md
```

PR body files pre-rendered: `pr_body_publicapis.md` and
`pr_body_apisguru.md` in this directory.
