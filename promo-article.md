# LinkPeek: 67 Free API Endpoints for URL Metadata, QR Codes, DNS, SSL & More — $0 Budget

**LinkPeek** is a free utility API suite running 67 endpoints — from URL link previews to DNS lookups, SSL certificate inspection, QR code generation, and more. No backend required. Built and hosted for $0 using an Oracle Cloud free-tier VPS + the Hermes autonomous agent framework.

---

## 🚀 What's Included

67 API endpoints across these categories:

- **Link Preview** — Fetch OpenGraph metadata, title, description, images for any URL
- **QR Codes** — Generate QR codes as PNG/SVG in any size or color
- **SSL Certificates** — Inspect any site's SSL cert: issuer, expiry dates, chain
- **DNS Lookup** — A, AAAA, MX, TXT, CNAME, NS records on demand
- **Geolocation** — IP Geolocation by IP address
- **URL Tools** — Expand short URLs, extract domains, validate URLs
- **Headers & Status** — Fetch HTTP response headers and status codes
- **Screenshots** — Capture website screenshots (PNG, Base64)
- **And many more...**

---

## 📡 Getting Started

### 1. Request a Free API Key

Send an email to the maintainer (or open a GitHub Discussion in the repo below) requesting a free API key. Keys are issued freely — no payment info needed.

### 2. Make Your First Request

```bash
curl -X POST http://147.15.103.217.sslip.io:5000/api/v1/link-preview \
  -H "Content-Type: application/json" \
  -H "X-API-Key: YOUR_FREE_KEY" \
  -d '{"url": "https://github.com"}'
```

**Response:**
```json
{
  "status": "success",
  "title": "GitHub: Let's build from here",
  "description": "GitHub is where over 100M developers shape the future of software.",
  "image": "https://github.githubassets.com/.../open-graph.png",
  "url": "https://github.com"
}
```

---

## 💻 Code Examples

### Python

```python
import requests

API_BASE = "http://147.15.103.217.sslip.io:5000"
API_KEY = "YOUR_FREE_KEY"

# Link Preview
resp = requests.post(
    f"{API_BASE}/api/v1/link-preview",
    json={"url": "https://example.com"},
    headers={"X-API-Key": API_KEY}
)
print(resp.json())

# QR Code
qr = requests.get(
    f"{API_BASE}/api/v1/qr-code",
    params={"data": "https://example.com", "size": "300"},
    headers={"X-API-Key": API_KEY}
)
with open("qr.png", "wb") as f:
    f.write(qr.content)

# DNS Lookup
dns = requests.get(
    f"{API_BASE}/api/v1/dns-lookup",
    params={"domain": "github.com", "record_type": "MX"},
    headers={"X-API-Key": API_KEY}
)
print(dns.json())
```

### JavaScript / Node.js

```javascript
const API_BASE = "http://147.15.103.217.sslip.io:5000";
const API_KEY = "YOUR_FREE_KEY";

// Link Preview
const resp = await fetch(`${API_BASE}/api/v1/link-preview`, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "X-API-Key": API_KEY,
  },
  body: JSON.stringify({ url: "https://example.com" }),
});
const data = await resp.json();
console.log(data);

// QR Code (as image)
const qr = await fetch(
  `${API_BASE}/api/v1/qr-code?data=https://example.com&size=300`,
  { headers: { "X-API-Key": API_KEY } }
);
const blob = await qr.blob();
console.log(`QR code size: ${blob.size} bytes`);
```

---

## 💰 Pricing

| Tier | Price | Quota |
|------|-------|-------|
| **Free** | $0 | 100 requests/day |
| **Pro** | $5/mo | 10,000 requests/day + priority |

Payment via PayPal. Cancel anytime — no lock-in.

---

## 🏗️ How It's Built

LinkPeek is fully open source and powered by:

- **Hermes Autonomous Agent Framework** — orchestration and taskrunning
- **Performer + 67 API tools** — a curated set of utility HTTP helpers
- **Oracle Cloud Free Tier** — host for $0/month
- **sslip.io** — dynamic DNS resolving to the VPS IP
- **Go-Auto-Deploy** — every git push to main deploys hot to production

No Kubernetes, no Terraform, no AWS bill. Just an agent, a VPS, and a release pipeline.

---

## 📎 Links

- **API Endpoint**: `http://147.15.103.217.sslip.io:5000`
- **Source Code**: [github.com/dcn13l/hermes-autonomia](https://github.com/dcn13l/hermes-autonomia)
- **Discussions**: [Open a Discussion](https://github.com/dcn13l/hermes-autonomia/discussions) to request a free key or report bugs

---

## 🔐 Getting a Key — Step by Step

1. Head to the [Discussions tab](https://github.com/dcn13l/hermes-autonomia/discussions) in the repo
2. Start a new discussion in "Q&A" or "Ideas" — title it **"Requesting free API key"**
3. Include a sentence on what you plan to build (so we can prioritize the right tools)
4. A key will be issued back to you within 24–48 hours

---

## 🎯 Use Cases

- **Link preview cards** in social-sharing widgets or chatbot embeds
- **QR codes** in printed flyers, event tickets, or restaurant menus
- **DNS / WHOIS lookups** in compliance or research pipelines
- **SSL monitoring** to surface certificate expiry before you get an alert
- **URL expansion** to de-mask abused shortlinks
- **Bulk IP→geo enrichment** from access logs

If any of these sound useful, grab a key and start building.

---

*LinkPeek is free, open source, and built with zero infrastructure budget. Source code is auditable and welcome to any improvement.*
