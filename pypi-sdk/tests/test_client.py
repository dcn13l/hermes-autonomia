"""Offline unit tests for the linkpeek client.

These do NOT hit the network — they mock responses with the `responses`
library so the suite stays hermetic and CI-fast. Run with::

    pip install linkpeek-api[test]
    pytest -q
"""

from __future__ import annotations

import json
import responses  # type: ignore
import pytest

import linkpeek
from linkpeek import DEFAULT_BASE_URL, LinkPeek, LinkPeekError


BASE = DEFAULT_BASE_URL


# ---------------------------------------------------------------------------
# Config / plumbing
# ---------------------------------------------------------------------------
def test_default_base_and_timeout():
    lp = LinkPeek()
    assert lp.base_url == BASE
    assert lp.api_key is None
    assert lp.timeout == linkpeek.DEFAULT_TIMEOUT


def test_custom_base_url_trailing_slash_stripped():
    lp = LinkPeek(base_url="http://localhost:5000/")
    assert lp.base_url == "http://localhost:5000"


def test_api_key_injected_into_qs():
    lp = LinkPeek(api_key="lp_pro_abc")
    qs = lp._qs({"url": "https://x"}, auth=True)
    assert qs == {"url": "https://x", "key": "lp_pro_abc"}


def test_qs_drops_none():
    lp = LinkPeek()
    qs = lp._qs({"url": "x", "wpm": None})
    assert qs == {"url": "x"}


def test_path_normalisation():
    assert linkpeek._BaseClient._path("preview") == "/api/preview"
    assert linkpeek._BaseClient._path("/api/preview") == "/api/preview"
    assert linkpeek._BaseClient._path("api/preview") == "/api/preview"


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------
@responses.activate
def test_preview_returns_json():
    responses.add(
        responses.GET, f"{BASE}/api/preview",
        json={"title": "Example", "description": "d", "quota": {"plan": "free"}},
        status=200,
    )
    lp = LinkPeek()
    out = lp.preview("https://example.com")
    assert out["title"] == "Example"
    # ensure ?url= went on the wire
    assert responses.calls[0].request.params["url"] == "https://example.com"


@responses.activate
def test_preview_attaches_pro_key():
    responses.add(
        responses.GET, f"{BASE}/api/preview",
        json={"title": "Ex"}, status=200,
    )
    lp = LinkPeek(api_key="lp_pro_zzz")
    lp.preview("https://x.com")
    assert responses.calls[0].request.params["key"] == "lp_pro_zzz"


@responses.activate
def test_qr_returns_png_bytes():
    responses.add(
        responses.GET, f"{BASE}/api/qr",
        body=b"\x89PNG\r\n\x1a\nFAKE",
        content_type="image/png",
        status=200,
    )
    lp = LinkPeek()
    data = lp.qr("https://example.com", ecc="H", fg="000000")
    assert data.startswith(b"\x89PNG")
    p = responses.calls[0].request.params
    assert p["text"] == "https://example.com"
    assert p["ecc"] == "H"


@responses.activate
def test_batch_joins_urls_with_commas():
    responses.add(
        responses.GET, f"{BASE}/api/batch",
        json={"results": []}, status=200,
    )
    LinkPeek().batch(["https://a.com", "https://b.com"])
    assert responses.calls[0].request.params["urls"] == "https://a.com,https://b.com"


@responses.activate
def test_shortlink_create_vs_resolve():
    responses.add(
        responses.GET, f"{BASE}/api/shortlink",
        json={"code": "ab12"}, status=200,
    )
    LinkPeek().shortlink(url="https://long.example/foo")
    assert responses.calls[0].request.params["url"] == "https://long.example/foo"


def test_shortlink_requires_arg():
    with pytest.raises(ValueError):
        LinkPeek().shortlink()


@responses.activate
def test_unmetered_endpoints_skip_key():
    responses.add(responses.GET, f"{BASE}/api/status", json={"version": "1.4.0"}, status=200)
    responses.add(responses.GET, f"{BASE}/api/health", json={"ok": True}, status=200)
    lp = LinkPeek(api_key="lp_pro_x")
    lp.status(); lp.health()
    # auth=False path → no ?key= on the wire
    for c in responses.calls:
        assert "key" not in c.request.params


@responses.activate
def test_trial_key_and_subscribe_have_no_key():
    responses.add(responses.GET, f"{BASE}/api/key", json={"key": "lp_trial_x"}, status=200)
    responses.add(
        responses.GET, f"{BASE}/api/subscribe",
        json={"api_key": "lp_pro_y", "pay_url": "https://buy.stripe.com/x"}, status=200,
    )
    lp = LinkPeek(api_key="lp_pro_should_not_attach")
    lp.trial_key("a@b.com")
    assert "key" not in responses.calls[0].request.params
    lp.subscribe("a@b.com")
    assert "key" not in responses.calls[1].request.params


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------
@responses.activate
def test_api_error_raises_with_payload():
    responses.add(
        responses.GET, f"{BASE}/api/preview",
        json={"error": "rate_limit_exceeded"}, status=429,
    )
    with pytest.raises(LinkPeekError) as ei:
        LinkPeek().preview("https://x.com")
    assert ei.value.status_code == 429
    assert "rate_limit" in str(ei.value)


@responses.activate
def test_http_error_without_json_body_still_messages_status():
    responses.add(
        responses.GET, f"{BASE}/api/preview",
        body="oops", status=500,
    )
    with pytest.raises(LinkPeekError) as ei:
        LinkPeek().preview("https://x.com")
    assert ei.value.status_code == 500
    assert "500" in str(ei.value)


@responses.activate
def test_invalid_json_response_raises():
    responses.add(
        responses.GET, f"{BASE}/api/preview",
        body="<not json>", status=200,
    )
    with pytest.raises(LinkPeekError):
        LinkPeek().preview("https://x.com")


# ---------------------------------------------------------------------------
# Low-level escape hatch
# ---------------------------------------------------------------------------
@responses.activate
def test_get_raw_for_undocumented_endpoint():
    responses.add(
        responses.GET, f"{BASE}/api/some-new-endpoint",
        json={"ok": True}, status=200,
    )
    lp = LinkPeek()
    out = lp.get("some-new-endpoint", params={"q": "x"})
    assert out == {"ok": True}
