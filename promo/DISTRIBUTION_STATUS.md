# LinkPeek distribution status — 2026-08-06

## Summary
- **Reddit (r/coolgithubprojects + r/SideProject): BLOCKED.** www.reddit.com returns HTTP 403 to unauthenticated curl/`submit.json`; old.reddit.com/submit also 403. No Reddit credentials in env (`REDDIT_*` absent). Browser is Chromium via snap but agent-browser can't launch it (launcher path has embedded null byte → terminal guard fails). No HTTP path to an authenticated Reddit session from this environment. Drafts in this dir are not posted.
- **Hacker News: BLOCKED (no login + low/zero karma).** reachable (200), but posting requires a logged-in browser session + account age/karma gating. Saved as a Show HN draft in `hackernews_comment.md` with a posting reality-check for the operator.
- **Dev.to: NOT POSTED (no API key).** `DEVTO_API_KEY` env var not set (`/articles` POST gets 401). Full expanded post written to `devto.md` with frontmatter ready to publish via `curl -X POST https://dev.to/api/articles -H "api-key: $DEVTO_API_KEY" -d@devto.md` once the operator sets the key.
- **Live API verified working.** `/api/preview?url=https://github.com` returns full JSON (title, description, og:image, favicon, site_name, quota object). `/api/qr?text=…` returns a 564-byte PNG (200 OK).
- **README improved** → written to `README_IMPROVEMENTS.md` (user-facing: quick start, endpoints table, pricing table, why-use, self-host, payments, ops). Existing README is ops-internal only.

## Files in this dir
| File | Purpose | Posted? |
|---|---|---|
| `reddit_coolgithubprojects.md` | Reddit draft (pre-existing) | NO — 403, no creds |
| `reddit_sideproject.md` | Reddit draft (pre-existing) | NO — 403, no creds |
| `devto.md` | Expanded Dev.to post w/ frontmatter | NO — no API key; ready to publish |
| `hackernews_comment.md` | Show HN draft + posting notes | NO — needs human HN account |
| `README_IMPROVEMENTS.md` | User-facing README rewrite | NOT applied to repo (clone at `/tmp/hermes-autonomia-fresh`) — operator to commit & push |
| `DISTRIBUTION_STATUS.md` | This file | — |

## What the operator needs to do to actually distribute
1. **Set `DEVTO_API_KEY`** (https://dev.to/settings/extensions → "DEV Community API Keys") then run:
   ```bash
   curl -X POST https://dev.to/api/articles \
     -H "api-key: $DEVTO_API_KEY" \
     -H "Content-Type: application/json" \
     -d "$(jq -Rs '{article: .}' < ~/.hermes/skills/autonomous-ai-agents/autonomous-business/product/promo/devto.md)"
   ```
   Then flip `published: true` in the frontmatter if you want it live immediately.
2. **Reddit**: log into your account in a normal browser, open https://www.reddit.com/r/coolgithubprojects/submit and https://www.reddit.com/r/SideProject/submit, paste from `reddit_coolgithubprojects.md` / `reddit_sideproject.md`. Don't post both at the same hour — stagger by ~24h to avoid both getting buried/flagged.
3. **Hacker News**: build a small karma buffer first (genuine comments on Show HN threads for ~1 week), then submit the Show HN title/text from `hackernews_comment.md` at https://news.ycombinator.com/submit. Posting Show HN from a zero-karma account usually gets throttled.
4. **README**: commit `README_IMPROVEMENTS.md` as the new repo README:
   ```bash
   cp /tmp/hermes-autonomia-fresh/README.md /tmp/hermes-autonomia-fresh/README.md.bak
   cp ~/.hermes/skills/autonomous-ai-agents/autonomous-business/product/promo/README_IMPROVEMENTS.md /tmp/hermes-autonomia-fresh/README.md
   cd /tmp/hermes-autonomia-fresh && git add README.md && git commit -m "README: user-facing quick start, endpoints, pricing, self-host" && git push
   ```
   (requires `gh auth` / push access to dcn13l/hermes-autonomia).

## Honest accounting
No posts were delivered to a public community from this run. The API was verified live; all marketing copy is written and ready; the blocker on every channel is **authentication credentials not present in this environment** (Reddit 403, Dev.to 401 without key, HN requires human account + karma). The operator must supply credentials or post from their own session. Nothing was invented.
