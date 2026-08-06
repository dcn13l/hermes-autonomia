# LinkPeek — Node.js quickstart snippet

Drop-in fetch usage for the LinkPeek link-preview + QR API.
No SDK install required (Node 18+ has global `fetch`).

## Link preview (JSON)

```js
const BASE = "http://147.15.103.217.sslip.io:5000";

async function preview(url, apiKey) {
  const qs = new URLSearchParams({ url, ...(apiKey ? { key: apiKey } : {}) });
  const r = await fetch(`${BASE}/api/preview?${qs}`);
  if (!r.ok) throw new Error(`LinkPeek ${r.status}: ${await r.text()}`);
  return r.json(); // { title, description, og:image, favicon, quota }
}

// usage
const meta = await preview("https://news.ycombinator.com");
console.log(meta.title, meta["og:image"]);
```

## QR code (PNG bytes)

```js
async function qr(text, opts = {}) {
  const qs = new URLSearchParams({
    text,
    ecc: opts.ecc ?? "M",
    ...(opts.fg ? { fg: opts.fg } : {}),
    ...(opts.bg ? { bg: opts.bg } : {}),
  });
  const r = await fetch(`${BASE}/api/qr?${qs}`);
  if (!r.ok) throw new Error(`LinkPeek ${r.status}: ${await r.text()}`);
  return Buffer.from(await r.arrayBuffer()); // write to file or pipe to res
}

import { writeFile } from "node:fs/promises";
await writeFile("qr.png", await qr("https://example.com"));
```

## Get a Pro key (self-serve)

```js
async function subscribe(email) {
  const qs = new URLSearchParams({ email });
  const r = await fetch(`${BASE}/api/subscribe?${qs}`);
  return r.json(); // { api_key, pay_url, pay_method, price_usd, instructions }
}
const { api_key, pay_url } = await subscribe("you@mail.com");
// api_key works immediately; pay_url is your Stripe/PayPal link.
```

## Same helpers as an `EventEmitter`-free mini-client

Copy this into your codebase — zero deps, ~40 lines:

```js
class LinkPeek {
  constructor({ apiKey, base = BASE, timeout = 15000 } = {}) {
    this.key = apiKey || null;
    this.base = base.replace(/\/$/, "");
    this.timeout = timeout;
  }
  async _get(endpoint, params = {}, { auth = true } = {}) {
    if (auth && this.key && !("key" in params)) params.key = this.key;
    const qs = new URLSearchParams(
      Object.fromEntries(Object.entries(params).filter(([, v]) => v != null))
    );
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), this.timeout);
    try {
      const r = await fetch(`${this.base}/api/${endpoint}?${qs}`, {
        signal: ctrl.signal,
      });
      if (!r.ok) {
        let body = await r.text();
        try { body = JSON.parse(body); } catch {}
        const msg = body?.error || body?.message || `HTTP ${r.status}`;
        throw Object.assign(new Error(msg), { status: r.status, payload: body });
      }
      const ct = r.headers.get("content-type") || "";
      if (ct.includes("application/json")) return r.json();
      return Buffer.from(await r.arrayBuffer());
    } finally { clearTimeout(t); }
  }
  preview(url)              { return this._get("preview", { url }); }
  extract(url)             { return this._get("extract", { url }); }
  opengraph(url)           { return this._get("opengraph", { url }); }
  metadata_full(url)       { return this._get("metadata-full", { url }); }
  batch(urls)              { return this._get("batch", { urls: urls.join(",") }); }
  diff(url1, url2)         { return this._get("diff", { url1, url2 }); }
  word_count(url, wpm)     { return this._get("word-count", { url, wpm }); }
  headers(url)             { return this._get("headers", { url }); }
  robots(url)              { return this._get("robots", { url }); }
  rss(url)                 { return this._get("rss", { url }); }
  favicon(url)             { return this._get("favicons", { url }); }
  qr(text, opts = {})      {
    return this._get("qr", { text, ecc: opts.ecc ?? "M", fg: opts.fg, bg: opts.bg });
  }
  shortlink({ url, code }) { return this._get("shortlink", { url, code }); }
  status()                 { return this._get("status", {}, { auth: false }); }
  health()                 { return this._get("health", {}, { auth: false }); }
  validate_key(key)         { return this._get("validate-key", { key }, { auth: false }); }
  trial_key(email)         { return this._get("key", { email }, { auth: false }); }
  subscribe(email)          { return this._get("subscribe", { email }, { auth: false }); }
}

export default LinkPeek; // or module.exports = LinkPeek;
```

Live API status & version: `GET /api/status` (unmetered).
