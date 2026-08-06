"""
linkpeek — thin Python client for the LinkPeek link-preview + QR-code API.

LinkPeek is a free (100 req/day, no key) HTTP API that unfurls links into
opengraph metadata, generates QR codes, and exposes 20 helper endpoints.
This module is a small ergonomic wrapper around plain ``requests`` calls so
you don't have to remember query-string names or messenger bytes-vs-json
returns.

Quickstart
----------
    from linkpeek import LinkPeek

    lp = LinkPeek()                          # free tier (per-IP rate limit)
    print(lp.preview("https://news.ycombinator.com")["title"])

    lp = LinkPeek(api_key="lp_pro_...")      # Pro: 50,000 req/day
    png = lp.qr("https://example.com", box_size=12)

The free tier needs no key at all. Grab a 14-day trial key with
``trial_key(email)`` or a permanent Pro key + payment link via
``subscribe(email)``.

Async
-----
    from linkpeek import LinkPeekAsync
    async with LinkPeekAsync() as lp:
        data = await lp.preview("https://example.com")

(Requires ``httpx``; ``pip install linkpeek-api[async]``.)
"""

from __future__ import annotations

import io
import json
from typing import Any, Dict, Iterable, Optional, Union

__all__ = ["LinkPeek", "LinkPeekAsync", "LinkPeekError", "__version__"]
__version__ = "0.1.0"

# Public default. Override per-instance if you self-host.
DEFAULT_BASE_URL = "http://147.15.103.217.sslip.io:5000"

# Default per-request timeout (seconds). Override per-call with ``timeout=``.
DEFAULT_TIMEOUT = 15


class LinkPeekError(Exception):
    """Raised when the API returns a non-2xx status or an ``{"error", ...}`` body."""

    def __init__(
        self, message: str, status_code: Optional[int] = None, payload: Any = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


def _bool(v: Any) -> str:
    """Coerce common python truthy/falsy values into ``"true"``/``"false"`` strings."""
    return "true" if v in (True, "true", "True", 1, "1") else "false"


def _raise_for(resp: Any, *, expect_json: bool) -> Any:
    """Shared response checker for the requests-based sync client.

    On non-2xx, attempt to parse a JSON error for the API's message field;
    fall back to the raw text so transport errors are still debuggable.
    """
    status = resp.status_code
    if not resp.ok:
        body: Any = resp.text
        try:
            body = resp.json()
        except (ValueError, json.JSONDecodeError):
            pass
        msg = (
            body.get("error") if isinstance(body, dict) and body.get("error") else None
        ) or (
            body.get("message")
            if isinstance(body, dict) and body.get("message")
            else None
        ) or (f"HTTP {status}")
        raise LinkPeekError(msg, status, body)

    if expect_json:
        try:
            return resp.json()
        except (ValueError, json.JSONDecodeError) as e:
            raise LinkPeekError(f"invalid JSON from API: {e}", status, resp.text)
    # Binary path (e.g. /api/qr PNG bytes).
    return resp.content


class _BaseClient:
    """Holds shared config; mixed in by both the sync and async clients."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # -- request plumbing helpers ---------------------------------------
    def _qs(
        self,
        params: Optional[Dict[str, Any]] = None,
        *,
        auth: bool = True,
    ) -> Dict[str, Any]:
        """Build the outgoing query string, optionally injecting the API key."""
        out: Dict[str, Any] = dict(params or {})
        if auth and self.api_key and "key" not in out:
            out["key"] = self.api_key
        # Drop None values so they don't become literal "None" strings.
        return {k: v for k, v in out.items() if v is not None}

    @staticmethod
    def _path(endpoint: str) -> str:
        """Normalise an endpoint token into an ``/api/...`` path."""
        endpoint = endpoint.lstrip("/")
        if not endpoint.startswith("api"):
            endpoint = f"api/{endpoint}"
        return "/" + endpoint


class LinkPeek(_BaseClient):
    """Synchronous LinkPeek client (uses ``requests``).

    Parameters
    ----------
    api_key : str, optional
        Pro or trial key. When provided it is attached as ``?key=`` to all
        metered endpoints, lifting the daily quota from 100 (free, per-IP)
        to 50,000 (Pro/trial).
    base_url : str
        Root of the LinkPeek deployment. Defaults to the public instance.
        Set this if you self-host.
    timeout : float
        Per-request timeout in seconds.
    session : requests.Session, optional
        Reuse an existing session (e.g. for connection pooling). The client
        closes any externally supplied session on ``close()`` only when
        it was constructed internally.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        session: Any = None,
    ) -> None:
        super().__init__(api_key=api_key, base_url=base_url, timeout=timeout)
        import requests  # local import keeps import-time deps honest

        self._requests = requests
        self._session = session or requests.Session()
        self._owns_session = session is None

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def __enter__(self) -> "LinkPeek":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- low-level get ---------------------------------------------------
    def get(
        self,
        endpoint: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        auth: bool = True,
        json_response: bool = True,
    ) -> Any:
        """Hit any ``/api/<endpoint>`` route and return parsed data.

        ``json_response=False`` returns raw bytes (use for ``/api/qr``,
        ``/api/favicons``). This is the single primitive every typed helper
        below calls, so it is also the escape hatch for new endpoints the
        typed helpers don't cover yet.
        """
        url = self.base_url + self._path(endpoint)
        qs = self._qs(params, auth=auth)
        resp = self._session.get(url, params=qs, timeout=self.timeout)
        return _raise_for(resp, expect_json=json_response)

    # -- link preview family --------------------------------------------
    def preview(self, url: str) -> Dict[str, Any]:
        """``/api/preview`` — title, description, og:image, favicon (+ quota echo)."""
        return self.get("preview", params={"url": url})

    def extract(self, url: str) -> Dict[str, Any]:
        """``/api/extract`` — deeper crawl: raw meta + up to 50 headings + 100 links."""
        return self.get("extract", params={"url": url})

    def metadata_full(self, url: str) -> Dict[str, Any]:
        """``/api/metadata-full`` — every meta tag in the document head."""
        return self.get("metadata-full", params={"url": url})

    def opengraph(self, url: str) -> Dict[str, Any]:
        """``/api/opengraph`` — OpenGraph-tag metadata only."""
        return self.get("opengraph", params={"url": url})

    oembed = metadata = opengraph  # backwards-compat alias (unfurl-style naming)

    def batch(self, urls: Iterable[str]) -> Dict[str, Any]:
        """``/api/batch`` — up to 5 URLs at once (parallel server-side fetch)."""
        joined = ",".join(urls)
        return self.get("batch", params={"urls": joined})

    def diff(self, url1: str, url2: str) -> Dict[str, Any]:
        """``/api/diff`` — structural comparison of two URLs' metadata."""
        return self.get("diff", params={"url1": url1, "url2": url2})

    # -- content & headers ----------------------------------------------
    def word_count(self, url: str, wpm: Optional[int] = None) -> Dict[str, Any]:
        """``/api/word-count`` — word count, reading time, top terms (+ optional ``wpm``)."""
        return self.get("word-count", params={"url": url, "wpm": wpm})

    def headers(self, url: str) -> Dict[str, Any]:
        """``/api/headers`` — HTTP response headers only."""
        return self.get("headers", params={"url": url})

    def robots(self, url: str) -> Dict[str, Any]:
        """``/api/robots`` — robots.txt parsed as JSON."""
        return self.get("robots", params={"url": url})

    def rss(self, url: str) -> Dict[str, Any]:
        """``/api/rss`` — RSS/Atom feed detection + parsing."""
        return self.get("rss", params={"url": url})

    def favicon(self, url: str) -> bytes:
        """``/api/favicons`` — raw favicon image bytes (proxied for ``<img>`` use)."""
        return self.get("favicons", params={"url": url}, json_response=False)

    # -- qr --------------------------------------------------------------
    def qr(
        self,
        text: str,
        *,
        ecc: str = "M",
        box_size: Optional[int] = None,
        border: Optional[int] = None,
        fg: Optional[str] = None,
        bg: Optional[str] = None,
    ) -> bytes:
        """``/api/qr`` — generate a QR code PNG (returns bytes).

        Parameters
        ----------
        text : str
            Payload to encode (max 2,000 chars server-side).
        ecc : str
            Error-correction level ``"L"`` / ``"M"`` / ``"Q"`` / ``"H"`` (default M).
        box_size, border : int, optional
            Server defaults apply when omitted.
        fg, bg : str, optional
            Hex colours without ``#`` (e.g. ``"000000"``); defaults black-on-white.

        Examples
        --------
        Save to disk::

            data = lp.qr("https://example.com")
            open("qr.png", "wb").write(data)

        Show inline in a notebook::

            from IPython.display import Image; Image(data)
        """
        params: Dict[str, Any] = {
            "text": text,
            "ecc": ecc,
            "box_size": box_size,
            "border": border,
            "fg": fg,
            "bg": bg,
        }
        return self.get("qr", params=params, json_response=False)

    def qr_image(self, text: str, **qr_kwargs: Any) -> Any:
        """Like :meth:`qr` but returns a wrapped ``BytesIO`` ready for Pillow::

            img = lp.qr_image("https://example.com")
            img.save("qr.png")

        Needs Pillow (``pip install Pillow``); imported lazily so the SDK
        doesn't require it at install time.
        """
        from PIL import Image  # type: ignore  # optional dep
        return Image.open(io.BytesIO(self.qr(text, **qr_kwargs)))

    # -- short links & discovery ----------------------------------------
    def shortlink(self, *, url: Optional[str] = None, code: Optional[str] = None) -> Dict[str, Any]:
        """``/api/shortlink`` — create a base62 short link (``url=``) or resolve one (``code=``)."""
        if not url and not code:
            raise ValueError("qr() needs either url= (create) or code= (resolve)")
        return self.get("shortlink", params={"url": url, "code": code})

    def status(self) -> Dict[str, Any]:
        """``/api/status`` — version + endpoint listing (unmetered discovery)."""
        return self.get("status", auth=False)

    def health(self) -> Dict[str, Any]:
        """``/api/health`` — `{ok, today:{day, count}}` liveness probe (unmetered)."""
        return self.get("health", auth=False)

    def validate_key(self, key: str) -> Dict[str, Any]:
        """``/api/validate-key`` — check whether a key is active and show its plan/quota."""
        return self.get("validate-key", params={"key": key}, auth=False)

    # -- key lifecycle (no auth needed) ---------------------------------
    def trial_key(self, email: str) -> Dict[str, Any]:
        """``/api/key?email=`` — issue a 14-day trial API key."""
        return self.get("key", params={"email": email}, auth=False)

    def subscribe(self, email: str) -> Dict[str, Any]:
        """``/api/subscribe?email=`` — Pro key + self-serve payment link (revenue path)."""
        return self.get("subscribe", params={"email": email}, auth=False)

    def screenshot_hint(self, url: str) -> Dict[str, Any]:
        """``/api/screenshot-url-hint`` — suggestion string for a hosted screenshot service."""
        return self.get("screenshot-url-hint", params={"url": url})


class LinkPeekAsync(_BaseClient):
    """Asyncio client (requires ``httpx``).

    ``LinkPeekAsync()`` returns a context manager that owns an
    ``httpx.AsyncClient``::

        async with LinkPeekAsync() as lp:
            data = await lp.preview("https://example.com")

    All sync helper signatures apply here too (``preview``, ``extract``,
    ``qr`` ...). Differences are confined to transport.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        client: Any = None,
    ) -> None:
        super().__init__(api_key=api_key, base_url=base_url, timeout=timeout)
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "LinkPeekAsync":
        if self._client is None:
            try:
                import httpx
            except ImportError as e:  # pragma: no cover
                raise ImportError(
                    "LinkPeekAsync needs httpx. "
                    "Install with: pip install linkpeek-api[async]"
                ) from e
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def get(
        self,
        endpoint: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        auth: bool = True,
        json_response: bool = True,
    ) -> Any:
        if self._client is None:
            # auto-initialise for callers that didn't use `async with`.
            await self.__aenter__()
        url = self._path(endpoint)
        qs = self._qs(params, auth=auth)
        resp = await self._client.get(url, params=qs)
        status = resp.status_code
        if not resp.is_success:
            body: Any = resp.text
            try:
                body = resp.json()
            except (ValueError, json.JSONDecodeError):
                pass
            err = body.get("error") if isinstance(body, dict) else None
            msg = str(err) if err else f"HTTP {status}"
            raise LinkPeekError(msg, status, body)
        if json_response:
            try:
                return resp.json()
            except (ValueError, json.JSONDecodeError) as e:
                raise LinkPeekError(
                    f"invalid JSON from API: {e}", status, resp.text
                )
        return resp.content

    # All typed helpers defined as `async def` mirrors of the sync client —
    # small enough that an explicit body is clearer than a metaclass-generated shim.
    async def preview(self, url: str) -> Dict[str, Any]:
        return await self.get("preview", params={"url": url})

    async def extract(self, url: str) -> Dict[str, Any]:
        return await self.get("extract", params={"url": url})

    async def metadata_full(self, url: str) -> Dict[str, Any]:
        return await self.get("metadata-full", params={"url": url})

    async def opengraph(self, url: str) -> Dict[str, Any]:
        return await self.get("opengraph", params={"url": url})

    async def batch(self, urls: Iterable[str]) -> Dict[str, Any]:
        return await self.get("batch", params={"urls": ",".join(urls)})

    async def diff(self, url1: str, url2: str) -> Dict[str, Any]:
        return await self.get("diff", params={"url1": url1, "url2": url2})

    async def word_count(self, url: str, wpm: Optional[int] = None) -> Dict[str, Any]:
        return await self.get("word-count", params={"url": url, "wpm": wpm})

    async def headers(self, url: str) -> Dict[str, Any]:
        return await self.get("headers", params={"url": url})

    async def robots(self, url: str) -> Dict[str, Any]:
        return await self.get("robots", params={"url": url})

    async def rss(self, url: str) -> Dict[str, Any]:
        return await self.get("rss", params={"url": url})

    async def favicon(self, url: str) -> bytes:
        return await self.get("favicons", params={"url": url}, json_response=False)

    async def qr(self, text: str, **kw: Any) -> bytes:
        params: Dict[str, Any] = {
            "text": text,
            "ecc": kw.get("ecc", "M"),
            "box_size": kw.get("box_size"),
            "border": kw.get("border"),
            "fg": kw.get("fg"),
            "bg": kw.get("bg"),
        }
        return await self.get("qr", params=params, json_response=False)

    async def shortlink(
        self, *, url: Optional[str] = None, code: Optional[str] = None
    ) -> Dict[str, Any]:
        if not url and not code:
            raise ValueError("shortlink() needs either url= or code=")
        return await self.get("shortlink", params={"url": url, "code": code})

    async def status(self) -> Dict[str, Any]:
        return await self.get("status", auth=False)

    async def health(self) -> Dict[str, Any]:
        return await self.get("health", auth=False)

    async def validate_key(self, key: str) -> Dict[str, Any]:
        return await self.get("validate-key", params={"key": key}, auth=False)

    async def trial_key(self, email: str) -> Dict[str, Any]:
        return await self.get("key", params={"email": email}, auth=False)

    async def subscribe(self, email: str) -> Dict[str, Any]:
        return await self.get("subscribe", params={"email": email}, auth=False)

    async def screenshot_hint(self, url: str) -> Dict[str, Any]:
        return await self.get("screenshot-url-hint", params={"url": url})
