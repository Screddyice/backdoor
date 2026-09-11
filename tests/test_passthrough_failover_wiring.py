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


class RecordingClient:
    """Records every request aimed at Anthropic, then fails the connection.

    Failing afterwards keeps these tests about ONE question — did the body
    leave for Anthropic at all — without needing a byte-faithful streaming
    relay to assert on. The local counter answers either way.
    """

    def __init__(self):
        self.requests = []

    def build_request(self, method, url, *, content, headers):
        return httpx.Request(
            method, f"https://api.anthropic.com{url}", content=content, headers=headers
        )

    async def send(self, request, *, stream):
        self.requests.append(request)
        raise httpx.ConnectTimeout("timed out")

    async def aclose(self):
        pass


def _recording_app(**overrides):
    recorder = RecordingClient()
    routes._upstream_client = recorder
    routes._breaker = None
    app = create_app()
    kwargs = {"router_mode": "hybrid", "failover_to_local": True, "failover_threshold": 99}
    kwargs.update(overrides)
    settings = Settings(**kwargs)
    app.dependency_overrides[get_settings] = lambda: settings
    return app, recorder


@pytest.mark.anyio
async def test_count_tokens_keeps_a_locally_routed_session_local():
    """A session routed to local weights must not ship its transcript to Anthropic.

    `/v1/messages` resolves its route with `resolve_model_route`, which lowers
    case on purpose — model names are identifiers a person types. `count_tokens`
    tested raw `MODEL_ROUTES` membership instead, so `/model Qwen` sent its
    COMPLETIONS to Ollama while relaying every count_tokens body — the whole
    conversation, on nearly every turn — to Anthropic.

    That is the worst shape a leak can take: the session is locally served,
    reports itself as locally served, and mirrors its transcript to a third
    party anyway.
    """
    app, recorder = _recording_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for model in ("Qwen", "qwen", "QWEN", " qwen "):
            resp = await client.post(
                "/v1/messages/count_tokens",
                json={"model": model,
                      "messages": [{"role": "user", "content": "a private transcript"}]},
            )
            assert resp.status_code == 200, model

    assert recorder.requests == [], (
        "a locally routed model must never relay count_tokens upstream; "
        f"{len(recorder.requests)} request(s) leaked"
    )


@pytest.mark.anyio
async def test_count_tokens_still_reaches_upstream_for_a_cloud_model():
    """The fix must not turn every count into a local estimate."""
    app, recorder = _recording_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/messages/count_tokens",
            json={"model": "claude-opus-5",
                  "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200

    # `_upstream_send` retries once internally, so the count is >= 1; the
    # property under test is that a cloud model still reaches Anthropic.
    assert recorder.requests, "a cloud model must still be counted upstream"

# ── Request bound ────────────────────────────────────────────────────────────
# The Codex relay has had a 64 MiB body cap since it was added; the Claude path
# read the whole body unbounded, so a loopback client could grow the router
# without limit. max_request_bytes applies the same bound here.


@pytest.fixture
def small_cap_app():
    app, settings = _app(max_request_bytes=128)
    try:
        yield app, settings
    finally:
        app.dependency_overrides.clear()
        routes._upstream_client = None
        routes._breaker = None


async def test_messages_over_the_byte_limit_gets_413(small_cap_app):
    app, _ = small_cap_app
    response = await _post(
        app,
        "/v1/messages",
        {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "x" * 512}]},
    )
    assert response.status_code == 413


async def test_count_tokens_over_the_byte_limit_gets_413(small_cap_app):
    app, _ = small_cap_app
    response = await _post(
        app,
        "/v1/messages/count_tokens",
        {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "x" * 512}]},
    )
    assert response.status_code == 413


async def test_catch_all_over_the_byte_limit_gets_413(small_cap_app):
    app, _ = small_cap_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/v1/anything", content=b"x" * 512)
    assert response.status_code == 413


async def test_a_body_under_the_limit_still_routes(small_cap_app):
    app, _ = small_cap_app
    response = await _post(
        app,
        "/v1/messages/count_tokens",
        {"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 200
    assert response.json()["input_tokens"] > 0
