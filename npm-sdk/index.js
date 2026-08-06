'use strict';

/**
 * linkpeek-api — minimal Node.js SDK for the LinkPeek API.
 *
 * Single-file, zero-dependency wrapper around https://147.15.103.217.sslip.io:5000.
 * Every method returns a Promise that resolves to the parsed JSON response,
 * or rejects with an Error containing the status code and body.
 *
 * Usage:
 *   const LinkPeek = require('linkpeek-api');
 *   const lp = new LinkPeek({ apiKey: 'your-key' });  // apiKey optional for free tier
 *   const preview = await lp.preview('https://example.com');
 *   const qr = await lp.qr('https://example.com');   // QR returns a Buffer (PNG bytes)
 */

const http = require('http');
const https = require('https');

const DEFAULT_BASE = 'https://147.15.103.217.sslip.io:5000';

class LinkPeek {
  /**
   * @param {object} opts
   * @param {string} [opts.baseURL]  Override the API base URL.
   * @param {string} [opts.apiKey]    API key (sent as ?key=). Free tier works without one.
   * @param {number} [opts.timeout]  Request timeout in ms (default 15000).
   */
  constructor(opts = {}) {
    this.baseURL = (opts.baseURL || DEFAULT_BASE).replace(/\/+$/, '');
    this.apiKey = opts.apiKey || process.env.LINKPEEK_API_KEY || '';
    this.timeout = opts.timeout || 15000;
  }

  /**
   * Low-level GET helper. Returns a Promise<{statusCode, headers, body}>.
   * For JSON endpoints body is the parsed object; for binary it's a Buffer.
   *
   * @param {string} path     e.g. '/api/preview'
   * @param {object} [params] Query-string params (key/value pairs).
   * @param {boolean} [json]  Parse body as JSON (default true).
   * @private
   */
  _get(path, params = {}, json = true) {
    return new Promise((resolve, reject) => {
      // Build query string, injecting the API key if set.
      const q = new URLSearchParams();
      if (this.apiKey) q.set('key', this.apiKey);
      for (const [k, v] of Object.entries(params)) {
        if (v === undefined || v === null) continue;
        q.set(k, String(v));
      }
      const qs = q.toString();
      const fullURL = this.baseURL + path + (qs ? '?' + qs : '');

      let parsed;
      try {
        parsed = new URL(fullURL);
      } catch (e) {
        return reject(new Error('Invalid URL: ' + fullURL));
      }

      const lib = parsed.protocol === 'https:' ? https : http;
      const req = lib.get(
        fullURL,
        {
          headers: { 'Accept': 'application/json, image/*' },
          timeout: this.timeout,
        },
        (res) => {
          const chunks = [];
          res.on('data', (c) => chunks.push(c));
          res.on('end', () => {
            const buf = Buffer.concat(chunks);
            const contentType = (res.headers['content-type'] || '').toLowerCase();

            if (json && contentType.includes('application/json')) {
              let body;
              try {
                body = JSON.parse(buf.toString('utf-8'));
              } catch (e) {
                return reject(new Error('JSON parse error: ' + e.message));
              }
              if (res.statusCode >= 400) {
                const errMsg = body.error || 'Request failed';
                const err = new Error(`LinkPeek ${res.statusCode}: ${errMsg}`);
                err.statusCode = res.statusCode;
                err.body = body;
                return reject(err);
              }
              return resolve(body);
            }

            // Binary (e.g. QR PNG) or non-JSON response
            if (res.statusCode >= 400) {
              const err = new Error(`LinkPeek ${res.statusCode}`);
              err.statusCode = res.statusCode;
              err.body = buf;
              return reject(err);
            }
            resolve({
              statusCode: res.statusCode,
              headers: res.headers,
              body: buf,
            });
          });
        }
      );

      req.on('error', (e) => reject(e));
      req.on('timeout', () => {
        req.destroy(new Error('Request timeout (' + this.timeout + 'ms)'));
      });
    });
  }

  // ---- Link preview & extraction -------------------------------------------

  /** GET /api/preview?url= — metered link-preview extraction. */
  preview(url) {
    return this._get('/api/preview', { url });
  }

  /** GET /api/extract?url= — deeper crawl: raw meta + links + headings. */
  extract(url) {
    return this._get('/api/extract', { url });
  }

  /** GET /api/metadata-full?url= — every meta tag, full dump. */
  metadataFull(url) {
    return this._get('/api/metadata-full', { url });
  }

  /** GET /api/meta-tags?url= — flat key→value map of head meta tags. */
  metaTags(url) {
    return this._get('/api/meta-tags', { url });
  }

  /** GET /api/opengraph?url= — strict OpenGraph fields, camelCased. */
  openGraph(url) {
    return this._get('/api/opengraph', { url });
  }

  /** GET /api/oembed?url= — oEmbed 1.0 "link" provider JSON. */
  oembed(url) {
    return this._get('/api/oembed', { url });
  }

  /** GET /api/links?url=&limit= — all links classified internal/external. */
  links(url, limit) {
    return this._get('/api/links', { url, limit });
  }

  /** GET /api/word-count?url=&wpm=&top= — content stats. */
  wordCount(url, opts = {}) {
    return this._get('/api/word-count', {
      url,
      wpm: opts.wpm,
      top: opts.top,
    });
  }

  // ---- Batch ----------------------------------------------------------------

  /**
   * GET /api/batch?url=...&url=... — up to 5 URLs at once.
   * @param {string[]} urls  Array of URLs (max 5).
   */
  batch(urls) {
    if (!Array.isArray(urls)) return Promise.reject(new Error('urls must be an array'));
    const params = {};
    urls.forEach((u, i) => (params['url' + (i ? i : '')] = u));
    // The API accepts ?url= repeated OR ?urls=a,b,c. We use the CSV form
    // for simplicity — it preserves order and handles the arg cleanly.
    return this._get('/api/batch', { urls: urls.join(',') });
  }

  // ---- Comparison -----------------------------------------------------------

  /** GET /api/diff?url1=&url2= — field-level diff of two URLs' metadata. */
  diff(url1, url2) {
    return this._get('/api/diff', { url1, url2 });
  }

  // ---- Utility endpoints ----------------------------------------------------

  /** GET /api/qr?text=&ecc=&fg=&bg= — QR code PNG. Returns a Buffer. */
  qr(text, opts = {}) {
    return this._get(
      '/api/qr',
      { text, ecc: opts.ecc, fg: opts.fg, bg: opts.bg },
      false
    ).then((r) => r.body); // return just the PNG Buffer
  }

  /** GET /api/favicons?url=&size= — favicon image bytes. Returns a Buffer. */
  favicon(url, size) {
    return this._get('/api/favicons', { url, size }, false).then((r) => r.body);
  }

  /** GET /api/robots?url= — robots.txt parsed as JSON. */
  robots(url) {
    return this._get('/api/robots', { url });
  }

  /** GET /api/headers?url= — HTTP response headers only. */
  headers(url) {
    return this._get('/api/headers', { url });
  }

  /** GET /api/rss?url= — detect + parse RSS/Atom feed. */
  rss(url) {
    return this._get('/api/rss', { url });
  }

  /** GET /api/screenshot-url-hint?url= — screenshot service URL suggestions. */
  screenshotHint(url) {
    return this._get('/api/screenshot-url-hint', { url });
  }

  // ---- Short links ----------------------------------------------------------

  /** GET /api/shortlink?url= — create a short link, returns {code, short_url}. */
  createShortlink(url) {
    return this._get('/api/shortlink', { url });
  }

  /** GET /api/shortlink?code= — resolve a short link, returns {original_url, hits}. */
  resolveShortlink(code) {
    return this._get('/api/shortlink', { code });
  }

  // ---- API key management ---------------------------------------------------

  /** GET /api/key?email= — issue a 14-day trial API key. */
  issueKey(email) {
    return this._get('/api/key', { email });
  }

  /** GET /api/validate-key?key= — check an API key's status. */
  validateKey(key) {
    return this._get('/api/validate-key', { key });
  }

  /** GET /api/subscribe?email= — self-serve Pro signup. */
  subscribe(email) {
    return this._get('/api/subscribe', { email });
  }

  // ---- Service status -------------------------------------------------------

  /** GET /api/status — service manifest, version, endpoint list. */
  status() {
    return this._get('/api/status', {});
  }

  /** GET /api/health — health check + daily totals + payment info. */
  health() {
    return this._get('/api/health', {});
  }
}

// CommonJS export — works with require() and also as a standalone script.
module.exports = LinkPeek;
module.exports.default = LinkPeek;
module.exports.LinkPeek = LinkPeek;
