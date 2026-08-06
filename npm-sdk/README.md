# linkpeek-api

> Minimal, zero-dependency Node.js SDK for the [LinkPeek](https://147.15.103.217.sslip.io:5000) link-preview API.

LinkPeek turns any URL into rich link-preview metadata (OpenGraph, title, description, image, favicon), generates QR codes, parses RSS feeds, and more — all from a simple REST API. This package wraps every endpoint in a clean Promise-returning client.

## Install

```bash
npm install linkpeek-api
# or
yarn add linkpeek-api
```

## Quick Start

```js
const LinkPeek = require('linkpeek-api');

// API key is optional for the free tier (100 req/day)
const lp = new LinkPeek({ apiKey: process.env.LINKPEEK_API_KEY });

// Preview a URL
const preview = await lp.preview('https://github.com');
console.log(preview.title, preview.image);

// Generate a QR code (returns a PNG Buffer)
const fs = require('fs');
const png = await lp.qr('https://example.com');
fs.writeFileSync('qr.png', png);

// Extract all links, classified as internal/external
const links = await lp.links('https://example.com');
console.log(links.internal_count, links.external_count);
```

## API

### `new LinkPeek(opts)`

| Option     | Type     | Default                          | Description                          |
| ---------- | -------- | -------------------------------- | ------------------------------------ |
| `baseURL`  | `string` | `https://147.15.103.217.sslip.io:5000` | Override the API base URL.     |
| `apiKey`   | `string` | `""`                             | API key (also settable via `LINKPEEK_API_KEY` env var). |
| `timeout`  | `number` | `15000`                          | Request timeout in milliseconds.     |

### Methods

All methods return a `Promise`. JSON endpoints resolve to the parsed response object; binary endpoints (`.qr()`, `.favicon()`) resolve to a `Buffer`.

| Method                       | API Endpoint              | Description                              |
| ---------------------------- | ------------------------- | ---------------------------------------- |
| `preview(url)`               | `/api/preview`            | Metered link-preview extraction.         |
| `extract(url)`               | `/api/extract`             | Deeper crawl: meta + links + headings.   |
| `metadataFull(url)`          | `/api/metadata-full`      | Every meta tag, full dump.               |
| `metaTags(url)`              | `/api/meta-tags`          | Flat key→value map of head meta tags.    |
| `openGraph(url)`             | `/api/opengraph`          | Strict OpenGraph fields, camelCased.      |
| `oembed(url)`                | `/api/oembed`             | oEmbed 1.0 "link" provider JSON.         |
| `links(url, [limit])`        | `/api/links`              | All links classified internal/external.  |
| `wordCount(url, [opts])`     | `/api/word-count`         | Content stats: word count, reading time. |
| `batch(urls[])`              | `/api/batch`              | Up to 5 URLs at once (parallel fetch).   |
| `diff(url1, url2)`           | `/api/diff`               | Field-level diff of two URLs' metadata. |
| `qr(text, [opts])`           | `/api/qr`                 | QR code PNG (returns `Buffer`).          |
| `favicon(url, [size])`       | `/api/favicons`           | Favicon image bytes (returns `Buffer`).  |
| `robots(url)`                | `/api/robots`             | robots.txt parsed as JSON.               |
| `headers(url)`               | `/api/headers`            | HTTP response headers only.              |
| `rss(url)`                   | `/api/rss`                | Detect + parse RSS/Atom feed.            |
| `screenshotHint(url)`        | `/api/screenshot-url-hint`| Screenshot service URL suggestions.      |
| `createShortlink(url)`       | `/api/shortlink?url=`     | Create a base62 short link.              |
| `resolveShortlink(code)`      | `/api/shortlink?code=`    | Resolve a short link.                    |
| `issueKey(email)`            | `/api/key`                | Issue a 14-day trial API key.            |
| `validateKey(key)`            | `/api/validate-key`       | Check API key status.                    |
| `subscribe(email)`            | `/api/subscribe`          | Self-serve Pro signup.                   |
| `status()`                   | `/api/status`             | Service manifest + endpoint list.        |
| `health()`                   | `/api/health`             | Health check + daily totals.             |

### Error Handling

Methods reject with an `Error` on non-2xx responses. The error has:
- `.statusCode` — the HTTP status code
- `.body` — the parsed JSON error body (for JSON responses) or `Buffer` (for binary)

```js
try {
  await lp.preview('not-a-url');
} catch (err) {
  console.error(err.statusCode, err.body.error);
}
```

## License

MIT
