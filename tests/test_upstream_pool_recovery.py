"""Regression coverage for a poisoned Anthropic connection pool."""

import json

import httpx
import pytest

import src.proxy.routes as routes
from src.proxy.app import create_app
from src.proxy.config import Settings, get_settings


class PoolTimedOutClient:
    """A pool whose slots stayed occupied after the host lost its network."""

    def __init__(self):
        self.closed = False

    def build_request(self, method, url, *, content, headers):
        return httpx.Request(method, f"https://api.anthropic.com{url}", content=content, headers=headers)

    async def send(self, request, *, stream):
        raise httpx.PoolTimeout("no connection became available")

    async def aclose(self):
        self.closed = True


def healthy_upstream() -> httpx.AsyncClient:
    def handler(_request: httpx.Request) -> httpx.Response:
        body = json.dumps({"content": [{"type": "text", "text": "recovered"}]}).encode()
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=httpx.ByteStream(body),
        )

    return httpx.AsyncClient(
        base_url="https://api.anthropic.com",
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture
def pool_timeout_app(monkeypatch):
    poisoned = PoolTimedOutClient()
    replacement = healthy_upstream()
    routes._upstream_client = poisoned
    monkeypatch.setattr(routes, "_new_upstream_client", lambda _settings: replacement, raising=False)

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        router_mode="hybrid",
        failover_to_local=False,
    )
    try:
        yield app, poisoned, replacement
    finally:
        app.dependency_overrides.clear()
        routes._upstream_client = None


async def test_pool_timeout_rotates_client_and_retries_once(pool_timeout_app):
    """A wedged shared pool must not hold every later Claude request hostage."""
    app, poisoned, replacement = pool_timeout_app
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/messages",
            content=json.dumps({"model": "claude-sonnet-5", "messages": []}).encode(),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 200
    assert response.json()["content"][0]["text"] == "recovered"
    assert poisoned.closed is True
    assert routes._upstream_client is replacement
