"""Passthrough handlers must survive an unreachable upstream, and feed the breaker.

Regression coverage for the 2026-08-24 `Connection dropped (ECONNRESET)`
incident. `/v1/messages` was guarded by `_try_upstream`, but three other paths
forwarded to Anthropic with no `except` around them:

  * `/v1/messages/count_tokens`
  * the `/{path:path}` catch-all
  * `/v1/messages` itself when `failover_to_local` is off

A transport error on any of them escaped the handler as an unhandled ASGI
exception, which uvicorn answers by dropping the client connection — the
ECONNRESET the user saw — and it never reached `record_failure`, so the
outage evidence from the busiest route (count_tokens fires nearly every turn)
was discarded and the breaker opened late or not at all.
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
        return httpx.Request(
            method, f"https://api.anthropic.com{url}", content=content, headers=headers
        )

    async def send(self, request, *, stream):
        raise httpx.ConnectTimeout("timed out")

    async def aclose(self):
        pass


def _app(**overrides):
    routes._upstream_client = ConnectTimeoutClient()
    routes._breaker = None  # rebuilt from these settings
    app = create_app()
    kwargs = {
        "router_mode": "hybrid",
        "failover_to_local": True,
        # High enough that a single failure cannot open the breaker: these tests
        # are about the failure being CAUGHT and COUNTED, not about opening.
        "failover_threshold": 99,
    }
    kwargs.update(overrides)
    settings = Settings(**kwargs)
    app.dependency_overrides[get_settings] = lambda: settings
    return app, settings


@pytest.fixture
def timing_out_app():
    app, settings = _app()
    try:
        yield app, settings
    finally:
        app.dependency_overrides.clear()
        routes._upstream_client = None
        routes._breaker = None


@pytest.fixture
def timing_out_app_no_failover():
    app, settings = _app(failover_to_local=False)
    try:
        yield app, settings
    finally:
        app.dependency_overrides.clear()
        routes._upstream_client = None
        routes._breaker = None


async def _post(app, path, payload):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            path,
            content=json.dumps(payload).encode(),
            headers={"content-type": "application/json"},
        )


async def test_count_tokens_falls_back_to_the_local_counter(timing_out_app, caplog):
    """An unreachable upstream must not fail a count — it is pure arithmetic."""
    app, settings = timing_out_app

    with caplog.at_level("WARNING", logger="src.proxy.routes"):
        response = await _post(
            app,
            "/v1/messages/count_tokens",
            {
                "model": "claude-sonnet-5",
                "messages": [{"role": "user", "content": "hello there"}],
            },
        )

    assert response.status_code == 200
    assert response.json()["input_tokens"] > 0
    assert "upstream transport failure (ConnectTimeout)" in caplog.text


async def test_count_tokens_failure_reaches_the_breaker(timing_out_app):
    """The busiest route must contribute its evidence, not swallow it."""
    app, settings = timing_out_app

    await _post(
        app,
        "/v1/messages/count_tokens",
        {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert routes.get_breaker(settings)._failures == 1


async def test_catch_all_returns_502_rather_than_dropping_the_connection(
    timing_out_app, caplog
):
    """No local equivalent exists, so the failure surfaces — but as a status."""
    app, settings = timing_out_app

    with caplog.at_level("WARNING", logger="src.proxy.routes"):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/v1/models")

    assert response.status_code == 502
    assert "upstream transport failure (ConnectTimeout)" in caplog.text
    assert routes.get_breaker(settings)._failures == 1


async def test_messages_without_failover_returns_502(
    timing_out_app_no_failover, caplog
):
    """failover_to_local=off opts out of the local model, not out of a response."""
    app, _ = timing_out_app_no_failover

    with caplog.at_level("WARNING", logger="src.proxy.routes"):
        response = await _post(
            app,
            "/v1/messages",
            {"model": "claude-sonnet-5", "max_tokens": 1, "messages": []},
        )

    assert response.status_code == 502
    assert "upstream transport failure (ConnectTimeout)" in caplog.text
