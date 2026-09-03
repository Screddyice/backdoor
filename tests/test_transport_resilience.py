"""Regression tests for transient cloud failures and local failover pressure."""

import asyncio
import json
import logging

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

import src.proxy.routes as routes
from src.proxy.client import ProviderClient
from src.proxy.config import Settings
from src.proxy.models import MessagesRequest


def _request(path: str = "/v1/messages") -> Request:
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8083),
    })


@pytest.mark.asyncio
async def test_transient_connect_failure_retries_once_before_reaching_the_client():
    attempts = []

    def fail_then_succeed(request: httpx.Request) -> httpx.Response:
        if not attempts:
            attempts.append("failed")
            raise httpx.ConnectTimeout("timed out", request=request)
        attempts.append("succeeded")
        return httpx.Response(200, content=b"ok", request=request)

    upstream = httpx.AsyncClient(
        base_url="https://api.anthropic.com",
        transport=httpx.MockTransport(fail_then_succeed),
    )
    routes._upstream_client = upstream

    try:
        response = await routes._upstream_send(_request(), b"{}", Settings())
        assert response.status_code == 200
        assert attempts == ["failed", "succeeded"]
        await response.aclose()
    finally:
        routes._upstream_client = None
        await upstream.aclose()


class _BlockedLocalHTTPClient:
    def __init__(self):
        self.calls = 0
        self.first_entered = asyncio.Event()
        self.release = asyncio.Event()

    async def post(self, path, json):
        self.calls += 1
        self.first_entered.set()
        await self.release.wait()
        return httpx.Response(200, json={"ok": True})

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_local_provider_serializes_requests_instead_of_flooding_ollama():
    provider = ProviderClient(Settings(provider_base_url="http://127.0.0.1:11434/v1"))
    await provider._client.aclose()
    blocked = _BlockedLocalHTTPClient()
    provider._client = blocked

    first = asyncio.create_task(provider.complete({"messages": []}))
    await blocked.first_entered.wait()
    second = asyncio.create_task(provider.complete({"messages": []}))
    await asyncio.sleep(0)

    assert blocked.calls == 1, "a retry opened another Ollama request while one was running"

    blocked.release.set()
    assert await first == {"ok": True}
    assert await second == {"ok": True}


# ── Local-provider transport failures ────────────────────────────────────────
# The upstream path has been hardened twice (`_guarded_passthrough`,
# `_relay_body`); the LOCAL provider path never was. `client.complete` and
# `client.stream` are wrapped in `except ProviderError`, which only covers an
# HTTP status the provider actually returned. An `httpx.TransportError` — in
# practice `ReadTimeout`, after the 600s read budget on a tier that is still
# prefilling — matches nothing and escapes to uvicorn, whose only answer is a
# bare 500 rendered by Claude Code as `API Error: 500 Internal server error`.
# Seen 62 times between 2026-08-26 and 2026-09-02, every one of them
# `routes.py → client.py:_complete → httpcore.ReadTimeout`.


def _messages_request(body: bytes, path: str = "/v1/messages") -> Request:
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8083),
    }, receive)


_BODY = json.dumps({
    "model": "claude-sonnet-5",
    "max_tokens": 16,
    "messages": [{"role": "user", "content": "hi"}],
}).encode()


def _read_timeout() -> httpx.ReadTimeout:
    return httpx.ReadTimeout(
        "local model still prefilling",
        request=httpx.Request("POST", "http://127.0.0.1:11434/chat/completions"),
    )


class _TimingOutLocalClient:
    async def complete(self, payload):
        raise _read_timeout()

    async def stream(self, payload):
        raise _read_timeout()
        yield {}  # pragma: no cover - unreachable, keeps this an async generator


@pytest.mark.asyncio
async def test_local_provider_read_timeout_answers_with_a_timeout_not_a_bare_500():
    routes.set_provider_client(_TimingOutLocalClient())
    try:
        with pytest.raises(HTTPException) as caught:
            await routes.create_message(_messages_request(_BODY), Settings())
    finally:
        routes.set_provider_client(None)
    assert caught.value.status_code == 504


@pytest.mark.asyncio
async def test_local_provider_stream_timeout_emits_an_error_event_not_a_dead_connection():
    req = MessagesRequest.model_validate_json(_BODY)
    events = []
    async for event in routes._stream(
        _TimingOutLocalClient(), {}, "msg_test", req, 10, "qwen-test"
    ):
        events.append(event)
    assert any("event: error" in event for event in events)


@pytest.mark.asyncio
async def test_relayed_upstream_error_status_reaches_the_log(caplog):
    upstream = httpx.AsyncClient(
        base_url="https://api.anthropic.com",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500, content=b"upstream boom", request=request)
        ),
    )
    routes._upstream_client = upstream
    try:
        with caplog.at_level(logging.WARNING, logger="src.proxy.routes"):
            response = await routes._upstream_send(_messages_request(b"{}"), b"{}", Settings())
        await response.aclose()
    finally:
        routes._upstream_client = None
        await upstream.aclose()
    assert any("500" in record.getMessage() for record in caplog.records)
