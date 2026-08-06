# linkpeek-api

Thin Python client for the **LinkPeek** link-preview + QR-code API.

LinkPeek unfurls any URL into opengraph metadata (title, description,
`og:image`, favicon), generates QR codes, and exposes 20 helper
endpoints — robots.txt parsing, oEmbed, RSS detection, batch fetch,
diff, headers-only, short links and more. Free tier: **100 req/day,
no key required**. Pro: **$5/mo, 50,000 req/day**.

```python
from linkpeek import LinkPeek

lp = LinkPeek()                                   # free, per-IP
meta = lp.preview("https://news.ycombinator.com")
print(meta["title"], meta.get("og:image"))

png = lp.qr("https://example.com", ecc="H")       # -> bytes
open("qr.png", "wb").write(png)

lp = LinkPeek(api_key="lp_pro_...")                # Pro tier
for u in ["https://a.com", "https://b.com"]:
    print(lp.extract(u)["title"])
```

## Install

```bash
pip install linkpeek-api              # sync client (requests)
pip install linkpeek-api[async]       # + httpx for asyncio
```

## API key

The free tier needs **no key** — calls are metered per IP. To go Pro:

```python
res = lp.subscribe("you@mail.com")
# -> {"api_key": "lp_pro_...", "pay_url": "...", "price_usd": 5, ...}
```

The returned `api_key` works **immediately**; `pay_url` is your hosted
Stripe / PayPal Me link. A 14-day trial key is available via
`lp.trial_key("you@mail.com")`.

## Endpoints covered

`preview` · `extract` · `metadata_full` · `opengraph` · `batch` ·
`diff` · `word_count` · `headers` · `robots` · `rss` · `favicon` ·
`qr` · `shortlink` · `sitemap` hint · `status` · `health` ·
`validate_key` · `trial_key` · `subscribe` · `screenshot_url_hint`

Every helper is a one-line wrapper over `lp.get(endpoint, params=...)`,
so any future endpoint works immediately via the low-level `get`.

## Async

```python
import asyncio
from linkpeek import LinkPeekAsync

async def main():
    async with LinkPeekAsync() as lp:
        data = await lp.preview("https://example.com")
        print(data["title"])

asyncio.run(main())
```

## Self-hosting

```python
lp = LinkPeek(base_url="http://localhost:5000")
```

Source & full HTTP docs: <https://github.com/dcn13l/hermes-autonomia>

## License

MIT
