# LinkPeek — autonomous-business revenue product

Flask app. Two tiers (matches `decorators.py`):

| Plan  | Daily limit  | Auth           | How to get                               |
|-------|-------------|----------------|------------------------------------------|
| Free  | 100 / day   | none (per-IP)  | just call `/api/preview?url=…`           |
| Pro   | 50,000 / day | API key        | `GET /api/subscribe?email=you@mail.com`  |
| Trial | 50,000 / day | API key        | `GET /api/key?email=you@mail.com` (14d)  |

## Endpoints
- `GET /api/preview?url=` — link preview JSON (title/description/og:image/favicon)
- `GET /api/qr?text=` — QR code PNG
- `GET /api/key?email=` — issue a 14-day **trial** API key
- `GET /api/subscribe?email=` — **revenue path**: mints a **Pro** API key (never
  expires, persisted to `keys.json`) and returns a self-serve **payment link**.
  Response includes `api_key`, `pay_url`, `pay_method`, `price_usd`, `instructions`.
- `GET /api/extract?url=` — raw meta + links + headings (deeper crawl)
- `GET /api/metadata-full?url=` — full metadata dump (every meta tag)
- `GET /api/batch?urls=` — up to 5 URLs at once (parallel fetch)
- `GET /api/diff?url1=&url2=` — compare two URLs' metadata
- `GET /api/favicons?url=` — proxy favicon image bytes
- `GET /api/robots?url=` — parse robots.txt as JSON
- `GET /api/headers?url=` — HTTP response headers only
- `GET /api/oembed?url=` — oEmbed 1.0 "link" provider JSON
- `GET /api/shortlink?url=` — create/resolve base62 short links
- `GET /lp/<code>` — 302 redirect for a short code
- `GET /api/screenshot-url-hint?url=` — suggestion for screenshot service URL
- `GET /api/opengraph?url=` — OpenGraph-only metadata extraction
- `GET /api/validate-key?key=` — validate an API key
- `GET /api/status` — version + endpoint listing (discovery)
- `GET /api/rss?url=` — RSS/Atom feed detection + parsing (NEW in 1.4.0)
- `GET /api/word-count?url=` — content stats: word count, reading time, top terms (NEW in 1.4.0)
- `GET /api/health` — `{ok, today:{day, count}}`

The Pro key works immediately at `/api/preview?key=<pro_key>` — `quota.limit`
jumps to 50,000. `paid:false` in `keys.json` is a reconciliation flag the operator
flips to `true` once the corresponding PayPal/Stripe notification arrives; the
key *already works* before then so the buyer gets value instantly.

## Turning on real payments ($0 budget, free to start)

`/api/subscribe` picks the first of these env vars that is set on the
`linkpeek.service` systemd unit and returns it as `pay_url`:

| Env var                 | Value to set                                 | Cost     |
|-------------------------|----------------------------------------------|----------|
| `LINKPEEK_STRIPE_LINK`  | `https://buy.stripe.com/<payment_link_id>`   | free to create, fee only on a sale |
| `LINKPEEK_PAYPAL_ME`    | `https://www.paypal.me/<your-username>`      | free to open (PayPal account) — no business account needed |
| *(neither set)*         | —                                            | `pay_method` falls back to `manual_email` (a `mailto:` to the operator). Still a working $0 path. |

Optional: `LINKPEEK_PRO_PRICE` (default `5`) — the monthly price in USD; used in
the PayPal Me URL (`paypal.me/u/5.00`) and the Stripe prefill.

### To activate Stripe (no spend until a sale, free to open account)
1. Sign up at https://dashboard.stripe.com/register (free; no card required).
2. Dashboard → **Payment Links** → **Create** → recurring product "$5 / mo",
   name it "LinkPeek Pro".
3. Copy the resulting `https://buy.stripe.com/…` link.
4. `sudo systemctl edit linkpeek.service` and add:
   ```
   [Service]
   Environment="LINKPEEK_STRIPE_LINK=https://buy.stripe.com/your_link_id"
   ```
5. `sudo systemctl restart linkpeek.service` — `/api/subscribe` now returns
   `pay_method:"stripe"` with `prefilled_email` appended for auto-reconciliation.

### To activate PayPal Me (fastest, free)
1. Sign up at https://www.paypal.com and grab a PayPal.Me link
   (`https://www.paypal.me/<username>`). Personal account, no business needed.
2. `sudo systemctl edit linkpeek.service`:
   ```
   [Service]
   Environment="LINKPEEK_PAYPAL_ME=https://www.paypal.me/your-username"
   ```
3. `sudo systemctl restart linkpeek.service`.

When a payment notification email arrives at the operator's inbox, find the row
in `product/keys.json` whose `email` matches the buyer and set `"paid":true`.

## Why this beats a RapidAPI listing (Option A comparison)
RapidAPI lets you list an API for free (they take a % per subscription sold
through the marketplace), then you publish a `POST /v1/preview` OpenAPI spec
and wait for **marketplace discovery** — approval is manual and traffic is
mostly internal search. Option B (this implementation) delivers revenue today:
a visitor hits `/api/subscribe`, gets a working Pro API key in the response,
and is sent straight to a hosted payment page. No listing review, no marketplace
cut, no per-transaction discovery lag. RapidAPI remains a worthwhile *secondary*
distribution channel once we have a production-ready spec.

## Quickstart (curl / Python / Node.js)

Base URL: `http://147.15.103.217.sslip.io:5000` · free tier = no key.

### curl

```bash
# Link preview (JSON)
curl "http://147.15.103.217.sslip.io:5000/api/preview?url=https://news.ycombinator.com"

# QR code (PNG bytes — pipe to a file)
curl "http://147.15.103.217.sslip.io:5000/api/qr?text=https://example.com" -o qr.png

# Discovery (unmetered — version + every endpoint)
curl "http://147.15.103.217.sslip.io:5000/api/status"

# Pro: attach ?key= to lift the daily quota to 50,000
curl "http://147.15.103.217.sslip.io:5000/api/preview?url=https://x.com&key=lp_pro_..."
```

### Python (`pip install linkpeek-api`)

```python
from linkpeek import LinkPeek

lp = LinkPeek()                              # free tier, no key
meta = lp.preview("https://news.ycombinator.com")
print(meta["title"], meta.get("og:image"))

png = lp.qr("https://example.com", ecc="H")  # -> bytes
open("qr.png", "wb").write(png)

# Go Pro (self-serve — key works immediately, pay_url is your Stripe/PayPal link):
sub = LinkPeek().subscribe("you@mail.com")
#  -> {"api_key": "lp_pro_...", "pay_url": "...", "price_usd": 5, ...}
```

SDK source: [`pypi-sdk/`](pypi-sdk/) · async client included (`linkpeek-api[async]`).

### Node.js (zero deps, Node 18+ global `fetch`)

```js
const BASE = "http://147.15.103.217.sslip.io:5000";

async function preview(url, apiKey) {
  const qs = new URLSearchParams({ url, ...(apiKey ? { key: apiKey } : {}) });
  const r = await fetch(`${BASE}/api/preview?${qs}`);
  if (!r.ok) throw new Error(`LinkPeek ${r.status}: ${await r.text()}`);
  return r.json();  // { title, description, og:image, favicon, quota }
}

const meta = await preview("https://news.ycombinator.com");
console.log(meta.title, meta["og:image"]);
```

Full mini-client class + QR/shortlink helpers: [`pypi-sdk/examples/node_quickstart.md`](pypi-sdk/examples/node_quickstart.md).

## Ops
- systemd unit: `/etc/systemd/system/linkpeek.service`
- nginx (public port 80) proxies → `127.0.0.1:5000` (the systemd app)
- `keys.json` (Pro/trial keys, persisted) — back this up; it is the customer record.
- `ledger_billable.md` — one row per billable call (the accounting ledger).
