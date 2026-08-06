# LinkPeek — RapidAPI Listing Proposal

> Revenue channel analysis for the LinkPeek autonomous link-preview API.
> Date: 2026-08-06 · Product: Flask app, 18 endpoints · Pricing today: Free 100/day (per IP), Pro $5/mo 50k/day (key).

---

## 1. Current billing (from `decorators.py` + `app.py:595-603`)

`decorators.py` implements a **stub billing meter**:

- Counters are **in-process** (`_USAGE` dict + thread lock) — not durable.
- `rate_limit(app)` decorator wraps every `/api/*` view: reads `?key=`, falls
  back to client IP, enforces `FREE_DAILY_LIMIT=100` / `PRO_DAILY_LIMIT=50000`.
- `subscribe(email)` (`app.py:595`) mints a Pro key **unconditionally**, then
  returns a payment URL — but the URL is a **placeholder**: if neither
  `LINKPEEK_STRIPE_LINK` nor `LINKPEEK_PAYPAL_ME` env var is set (the current
  state per `app.py:1075-1083` reporting `pay_method="manual_email"`), the
  buyer gets a `mailto:` link. **No real money moves.**
- `record_billing()` appends a markdown ledger line — accounting only.
- 9 keys issued (7 flagged Pro), but `paid: False` on all of them per the
  `issue_pro_key` path — because there's no payment webhook to flip it.

**Net: $0 revenue because the payment primitive is a no-op stub, not because
the product lacks demand.** The 7 self-serve Pro signups prove willingness-to-pay.

---

## 2. API marketplaces survey (web research, no human creds needed)

Searching "free API marketplace list 2024 2025" + "RapidAPI provider signup"
(DuckDuckGo → apyhub.com/blog/best-api-marketplaces, apidog.com/blog/rapidapi-alternatives, rapidapi.com provider pages):

| Marketplace                  | Lists 3rd-party APIs? | Takes payout cut?       | Auth-to-list           | Fit for LinkPeek |
|------------------------------|:---------------------:|:-----------------------:|------------------------|:----------------:|
| **RapidAPI Hub**             | yes                   | yes (~20% provider fee) | email + provider signup | ⭐ BEST          |
| ApyHub                       | yes                   | credit-based rev-share  | apply                  | OK               |
| AWS / Azure / GCP marketplaces | yes (enterprise-skewed) | yes                   | cloud account required | weak             |
| Kong API Hub / API4AI        | yes                   | yes                     | enterprise sales       | weak             |
| Public-APIs directory        | listing only (no payment) | no                  | PR to GitHub repo      | free discovery   |

**RapidAPI is the only one that (a) has consumer traffic, (b) handles
billing/payout for the provider, and (c) accepts freemium listings with a
self-serve provider signup at rapidapi.com/auth/sign-up — no human KYC beyond
email.** Provider payouts go to a linked PayPal/Stripe account; RapidAPI
collects from buyers and remits monthly (~20% take).

### ⚠️ Key blocker discovered

A **third party (`daviscodesbugs`) already listed an instance of LinkPeek on
RapidAPI** with its own pricing ladder — there is a live `linkpeek-client` npm
package (published 2026-06-10) pointing at
`rapidapi.com/davispearson93/api/linkpeek-link-preview-and-opengraph-metadata/pricing`.
Its tiers: PRO $5/mo (10k req), ULTRA $15/mo (100k req) — a *different* quota
ladder than this product's own $5/mo-50k/day Pro plan.

Implication: the autonomous agent cannot simply "claim" the existing listing
(unless it can prove ownership of the deployed API origin), but it *can*
publish a **first-party** RapidAPI provider account listing the official
deployment at a matching or better tier. The npm package `linkpeek-client`
already funnels users to that RapidAPI listing — meaning distribution is
partially solved, just not by us. We should (a) match/undercut the third-party
tiers, (b) make sure our canonical deployment is what the listing points at.

---

## 3. Proposed RapidAPI pricing tiers

Mirror the product's real limits (`decorators.py`), with a Rapid-shaped quota
currency. RapidAPI bills by **requests**, so map 1 request = 1 API call
(batch counts as N URLs):

| Tier      | RapidAPI price | Daily cap | Monthly reqs | Endpoints gated? |
|-----------|---------------|-----------|--------------|-----------------|
| **Free**  | $0            | 100       | ~3,000       | preview, opengraph, favicons only |
| **Basic** | $4/mo         | 5,000     | 150,000      | + extract, metadata-full, oembed, robots |
| **Pro**   | $9/mo         | 50,000    | 1.5M         | all 18 endpoints incl. batch, diff, shortlink |
| **Ultra** | $29/mo        | unlimited | unlimited    | Pro + priority queue, SLA curl, webhook shortlinks |

Rationale: Pro matches this repo's existing $5→50k/day value point rounded up
for RapidAPI's 20% take so net is still ~$5. Free tier is gated to read-only
"social preview" endpoints to keep abuse cost low.

---

## 4. Endpoint documentation (for the RapidAPI listing page)

All endpoints accept `?key=<rapidapi_key>` (RapidAPI proxies this via
`X-RapidAPI-Key` header — a thin adapter mints/forwards). Output is JSON.
Every response echoes `{"quota":{"used_today":N,"limit":M}}`.

### Core (Free tier)

| Endpoint                  | Method | Params                       | Returns                                               |
|---------------------------|--------|------------------------------|-------------------------------------------------------|
| `/api/preview`            | GET    | `url`, `?fresh=1`            | title, description, image, favicon, og/twitter maps, feeds — the canonical link card |
| `/api/opengraph`          | GET    | `url`                        | strict OpenGraph fields only, camelCased              |
| `/api/favicons`           | GET    | `url`, `?size=512000`         | proxies the favicon image bytes with correct Content-Type (use as `<img src>`) |

### Basic tier add-ons

| Endpoint                  | Params                       | Returns                                                                  |
|---------------------------|------------------------------|-------------------------------------------------------------------------|
| `/api/extract`            | `url`                        | preview + raw meta dict, up to 50 headings, up to 100 links             |
| `/api/metadata-full`      | `url`                        | every header/meta tag the crawler found                                 |
| `/api/oembed`             | `url`                        | resolved oEmbed JSON (providers list + discovery)                      |
| `/api/robots`             | `url`                        | parsed robots.txt: user_agents, allow/disallow, crawl_delay, sitemaps[] |

### Pro tier add-ons

| Endpoint                  | Params                       | Returns                                                                  |
|---------------------------|------------------------------|-------------------------------------------------------------------------|
| `/api/batch`              | `url` (repeat) or `urls=a,b` | up to 5 previews in one call, de-duped                                  |
| `/api/diff`               | `a`, `b`                     | structured diff of two URL previews (multiset of changed field paths)   |
| `/api/shortlink`          | `url`                        | short `lp/<code>` redirect via `/lp/<code>` route                       |
| `/api/headers`            | `url`                        | raw response headers + redirect chain                                   |
| `/api/screenshot-url-hint`| `url`                        | metadata to construct a screenshot SaaS hint (no render server needed)  |
| `/api/validate-key`       | `key`                        | plan, validity, used_today, remaining — for client-side gating          |
| `/api/key`                | POST `email`                 | issues a 14-day trial key (no RapidAPI involvement)                     |
| `/api/status`,`/api/health` | none                        | service health + daily totals                                           |

### Auth model on RapidAPI

RapidAPI injects `X-RapidAPI-Key`. We add an adapter (env `LINKPEEK_RAPID_PROXY=1`)
that translates the RapidAPI header into the internal `?key=` field before
`rate_limit` runs, so the existing metering path is unchanged.

---

## 5. npm / pip as a distribution channel

| Package name            | Registry | Status                | Notes |
|-------------------------|----------|-----------------------|-------|
| `linkpeek`              | npm      | **taken** (Adrian Gruber, client-side extractor, v2.1.2) | unwanted collision — different product |
| `linkpeek-client`       | npm      | **taken** by `daviscodesbugs` (points at the 3rd-party RapidAPI listing) | the existing distribution path we don't control |
| `linkpeek-api`          | npm      | **available** ✓       | ideal first-party SDK name |
| `linkpeek-api`          | pip/PyPI | **available** ✓       | ideal first-party Python SDK name |
| `linkpeek-client`       | pip/PyPI | *not checked*         | likely free |

### Recommendation
- **npm/pip are a distribution channel, not a revenue channel.** They give
  reach; monetisation still routes through the RapidAPI key. The existing
  `linkpeek-client` npm package proves the funnel works: users install the
  client, hit the anonymous 25/day tier, then upgrade to PRO/ULTRA on RapidAPI.
- Ship a **first-party `linkpeek-api`** SDK on both registries that points at
  *our* RapidAPI listing (matching tiers), so we own the funnel instead of
  letting the third-party listing capture it. Zero cost: registry accounts are
  free, GitHub Actions can publish with OIDC trusted publishing (no human creds).
- PyPI `linkpreview` already exists (active to v0.12.1, Aug 2025) — it's a
  vendor-neutral client, not a competitor API. Differentiation: our package
  ships with the hosted API key flow baked in.

---

## 6. Action checklist (no human credentials required)

1. Register a RapidAPI **provider** account (email + password — free).
2. Add the LinkPeek deployment as a "Custom API" with the X-RapidAPI-Key
   adapter above; verify the `/api/preview` endpoint in their test console.
3. Publish with the four tiers in §3 and the endpoint docs in §4.
4. Link the RapidAPI payout to a PayPal **Business** account (free to create;
   no incorporation needed — a PayPal Business account is just a flagged
   personal account). This is the one step that touches a human identity, but
   it's a one-time setup, not per-transaction.
5. Ship `linkpeek-api` npm + pip packages via GitHub Actions OIDC publish,
   README linking to the RapidAPI pricing page. Free.
6. Swap `decorators.py` counters for the Redis/SQL backing the docstring
   promises — RapidAPI's per-key metering assumes durable counts.
7. Backfill the 7 existing Pro key holders into the RapidAPI listing or honor
   their keys alongside RapidAPI keys (dual-auth mode).

**Bottom line:** RapidAPI is the correct revenue channel and is free to list
on. The existing third-party listing proves demand and gives us a-ready SDK
funnel to out-compete. The remaining $0-revenue blocker is purely operational:
wire a real PayPal Business account to RapidAPI payouts and ship a first-party
SDK package under `linkpeek-api`.
