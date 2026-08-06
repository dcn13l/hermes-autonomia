# LinkPeek

A free, open-source **link preview API** and **QR code API**. Drop in a URL, get back OpenGraph metadata (title, description, OG image, favicon, site name) as JSON — plus a one-call QR code endpoint that returns a PNG.

Live demo: **http://147.15.103.217.sslip.io:5000**

## Quick start

No signup for the free tier — just call the endpoint.

```bash
# Link preview
curl "http://147.15.103.217.sslip.io:5000/api/preview?url=https://github.com"
```
```json
{
  "title": "GitHub · Change is constant. GitHub keeps …",
  "description": "Join the world's most widely adopted, AI-powered developer platform…",
  "image": "https://images.ctfassets.net/8aevphvgewt8/…/GH-Homepage-Universe-img.png",
  "favicon": "https://github.com/fluidicon.png",
  "site_name": "GitHub",
  "quota": { "limit": 100, "used_today": 1 }
}
```

```bash
# QR code
curl "http://147.15.103.217.sslip.io:5000/api/qr?text=https://example.com" --output qr.png
```

## Endpoints

| Endpoint | What it returns | Auth |
|---|---|---|
| `GET /api/preview?url=…` | JSON: `title`, `description`, `image`, `favicon`, `site_name`, `quota` | none (free) or `?key=` (Pro/trial) |
| `GET /api/qr?text=…` | PNG QR code (200×200) | none (free) or `?key=` (Pro/trial) |
| `GET /api/key?email=…` | Issues a **14-day trial** API key (50k req/day) | none |
| `GET /api/subscribe?email=…` | Isssues a **permanent Pro** API key + returns a payment link | none |
| `GET /api/health` | `{ ok, today: { day, count } }` | none |

## Pricing

| Tier | Daily limit | Auth | Price |
|---|---|---|---|
| **Free** | 100 requests | none (per-IP) | $0 — no signup |
| **Trial** | 50,000 requests | 14-day API key from `/api/key?email=…` | $0 |
| **Pro** | 50,000 requests | permanent API key from `/api/subscribe?email=…` | $5/mo |

The `quota` object is included in every `/api/preview` and `/api/qr` response so you can show your users how much they have left, or switch a request from free to Pro by appending `?key=<your_key>` — no code change.

## Why use this?

- **CORS proxy** — your browser app can't read `https://example.com`'s HTML cross-origin. This API can.
- **Reliable OG extraction** — handles sites that bury `og:image`, only have Twitter Cards, or block scraper IPs with appropriate fetch headers.
- **Favicon fallback** — when there's no OG image, you still get a working favicon so your UI isn't blank.
- **Hosted + self-hostable** — use the live endpoint for a prototype, then clone the repo and run it in your own VPC when you outgrow the free tier. ~200 lines of Python.

## Self-host

```bash
git clone https://github.com/dcn13l/hermes-autonomia
cd hermes-autonomia
pip install -r requirements.txt
flask run
```

The app is a single small Flask codebase:
- `app.py` — endpoint logic
- `decorators.py` — rate-limit / Pro-key gate
- `keys.json` — Pro & trial key record (back this up — it is your customer list)
- `index.html` — landing page

For production it runs behind systemd + nginx; see the "Ops" section below.

## Stack

Flask · BeautifulSoup (HTML parsing) · Flask-Limiter (rate limiting) · Werkzeug dev server (systemd + nginx in prod). No database — the customer record is a JSON file, which keeps the operational surface tiny for a hobby-scale API that's meant to grow.

## Turning on payments ($0 to start)

`/api/subscribe` picks the first of these env vars set on the systemd unit and returns it as `pay_url`:

| Env var | Value | Cost |
|---|---|---|
| `LINKPEEK_STRIPE_LINK` | `https://buy.stripe.com/<payment_link_id>` | free to create; fee only on a sale |
| `LINKPEEK_PAYPAL_ME` | `https://www.paypal.me/<username>` | free to open a PayPal account |
| *(neither set)* | — | falls back to `mailto:` to the operator (still a working $0 path) |

Optional: `LINKPEEK_PRO_PRICE` (default `5`) — the monthly price in USD.

### Activate Stripe (no spend until a sale)
1. Sign up at https://dashboard.stripe.com/register (free, no card required).
2. Dashboard → **Payment Links** → **Create** → recurring product "$5 / mo", name it "LinkPeek Pro".
3. Copy the `https://buy.stripe.com/…` link.
4. `sudo systemctl edit linkpeek.service` → add:
   ```
   [Service]
   Environment="LINKPEEK_STRIPE_LINK=https://buy.stripe.com/your_link_id"
   ```
5. `sudo systemctl restart linkpeek.service`.

### Activate PayPal Me (fastest)
1. Get a `https://www.paypal.me/<username>` link (personal account, no business needed).
2. `sudo systemctl edit linkpeek.service` → add:
   ```
   [Service]
   Environment="LINKPEEK_PAYPAL_ME=https://www.paypal.me/your-username"
   ```
3. `sudo systemctl restart linkpeek.service`.

When a payment notification email arrives, find the row in `keys.json` whose `email` matches the buyer and set `"paid":true`. The Pro key **already works** before then — `paid:false` is just a reconciliation flag.

## Ops

- systemd unit: `/etc/systemd/system/linkpeek.service`
- nginx (public port 80) proxies → `127.0.0.1:5000`
- `keys.json` — Pro/trial keys, persisted. **Back this up** — it is the customer record.
- `ledger_billable.md` — one row per billable call (accounting ledger).

## Why this beats a RapidAPI listing

RapidAPI lets you list an API for free (they take a % per marketplace sale), but approval is manual, traffic is mostly internal search, and you wait weeks for discovery. This direct path delivers revenue the same day: a visitor hits `/api/subscribe`, gets a working Pro API key in the response, and is sent straight to a hosted payment page. No listing review, no marketplace cut. RapidAPI is a worthwhile *secondary* channel once there's a production-ready OpenAPI spec.

## License & contributing

Open source — see the repo. Issues welcome, especially URLs where OG image extraction fails (that's the genuinely fiddliest part). PRs welcome for: PDF cover previews, oEmbed fallback, batch preview requests, Twitter Card fallback.

---

Built solo. Free tier is actually free. If you ship it somewhere real, $5/mo. That's the whole pitch.
