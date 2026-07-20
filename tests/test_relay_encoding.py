"""Regression tests for content-encoding handling on the failover error-relay.

When Anthropic returns a below-threshold FAILOVER_STATUS (e.g. a lone 429 of
normal rate-limit backpressure), the router relays that error to the client
verbatim so the client's own retry/backoff still runs. The upstream body is
read with ``httpx.Response.aread()``, which *undoes* content-encoding — so the
relayed response must NOT keep the upstream ``content-encoding: gzip`` header,
or a gzip-negotiating client (Claude Code sends ``accept-encoding: gzip``) will
try to gunzip already-decoded plaintext and raise a decompression error.

The streaming success path (`_relay_upstream`) is unaffected: it forwards raw,
still-encoded bytes via ``aiter_raw()`` and truthfully keeps content-encoding.
"""

import gzip
import json

import httpx
import pytest

import src.proxy.routes as routes
from src.proxy.app import create_app
from src.proxy.config import Settings, get_settings

_ERR_PAYLOAD = json.dumps(
    {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}
).encode()


def _gzip_429_upstream() -> httpx.AsyncClient:
    """A mock upstream that answers every request with a gzip-compressed 429,
    exactly as Anthropic does under rate-limit backpressure."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={
                "content-encoding": "gzip",
                "content-type": "application/json",
                "retry-after": "5",
            },
            content=gzip.compress(_ERR_PAYLOAD),
        )

    return httpx.AsyncClient(
        base_url="https://api.anthropic.com",
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture
def app_with_gzip_429():
    """FastAPI app in hybrid mode whose upstream returns a below-threshold
    gzip 429 (threshold=3 ⇒ a single 429 is relayed verbatim, not failed over)."""
    routes._upstream_client = _gzip_429_upstream()
    routes._breaker = None
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        router_mode="hybrid", failover_to_local=True, failover_threshold=3
    )
    try:
        yield app
    finally:
        app.dependency_overrides.clear()
        routes._upstream_client = None
        routes._breaker = None


async def _post(app) -> httpx.Response:
    body = json.dumps(
        {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "hi"}]}
    ).encode()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # A gzip-negotiating client, like Claude Code.
        return await client.post(
            "/v1/messages",
            content=body,
            headers={"accept-encoding": "gzip", "content-type": "application/json"},
        )


async def test_below_threshold_gzip_error_relayed_without_content_encoding(app_with_gzip_429):
    # Reading the response must NOT raise a decompression error, and the client
    # must see the decoded error JSON — this is the exact symptom being fixed.
    resp = await _post(app_with_gzip_429)

    assert resp.status_code == 429
    assert "content-encoding" not in {k.lower() for k in resp.headers}
    assert json.loads(resp.content) == json.loads(_ERR_PAYLOAD)
    # Status-specific headers the client relies on for backoff are preserved.
    assert resp.headers.get("retry-after") == "5"


def test_decoded_relay_headers_drop_content_encoding():
    """Unit lock on the header contract: an already-decoded body may not carry a
    content-encoding header, and content-length is left for Starlette to recompute."""
    uresp = httpx.Response(
        429,
        headers={
            "content-encoding": "gzip",
            "content-type": "application/json",
            "retry-after": "5",
            "content-length": "999",
        },
        content=gzip.compress(_ERR_PAYLOAD),
    )
    hdrs = {k.lower(): v for k, v in routes._decoded_relay_headers(uresp).items()}
    assert "content-encoding" not in hdrs
    assert "content-length" not in hdrs
    assert hdrs["content-type"] == "application/json"
    assert hdrs["retry-after"] == "5"
