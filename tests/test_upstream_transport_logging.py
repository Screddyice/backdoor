"""Every upstream transport failure must leave a log line.

Regression coverage for the 2026-08-20 VPN incident: isolated ConnectTimeouts
below the breaker threshold returned bare 502s, so the retry banners the user
saw had no counterpart anywhere in the router log.
"""

import json

import httpx
import pytest

import src.proxy.routes as routes
from src.proxy.app import create_app
from src.proxy.config import Settings, get_settings


class ConnectTimeoutClient:
    """An upstream whose new connections never complete."""

    def build_request(self, method, url, *, content, headers):
        return httpx.Request(method, f"https://api.anthropic.com{url}", content=content, headers=headers)

    async def send(self, request, *, stream):
        raise httpx.ConnectTimeout("timed out")

    async def aclose(self):
        pass


@pytest.fixture
def timing_out_app():
    routes._upstream_client = ConnectTimeoutClient()
    routes._breaker = None  # rebuilt from this test's settings

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        router_mode="hybrid",
        failover_to_local=True,
        failover_threshold=5,
    )
    try:
        yield app
    finally:
        app.dependency_overrides.clear()
        routes._upstream_client = None
        routes._breaker = None


async def test_below_threshold_transport_failure_logs_and_502s(timing_out_app, caplog):
    transport = httpx.ASGITransport(app=timing_out_app)

    with caplog.at_level("WARNING", logger="src.proxy.routes"):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/v1/messages",
                content=json.dumps({"model": "claude-sonnet-5", "messages": []}).encode(),
                headers={"content-type": "application/json"},
            )

    assert response.status_code == 502
    assert "upstream transport failure (ConnectTimeout)" in caplog.text
