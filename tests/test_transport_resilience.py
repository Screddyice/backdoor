"""Regression tests for transient cloud failures and local failover pressure."""

import asyncio
import json
import logging
import threading

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


# ── The breaker's probe, and what may be replayed ────────────────────────────
# Two faults with one shape: `record_failure` runs `internet_reachable`, a
# blocking socket probe, and `_try_upstream` called it straight from the event
# loop, so every failed turn froze the router for the length of the probe. The
# same synchronous call also let a slow probe finish AFTER a newer request had
# succeeded and write its stale verdict over that success.
#
# Separately, `_upstream_send` replayed every httpx.TransportError once. A
# connect or pool failure is safe to replay because nothing reached Anthropic.
# A read, write or protocol error is not: the request may already be on their
# side, and replaying it can duplicate a turn.

@pytest.mark.asyncio
async def test_response_side_transport_failure_is_not_replayed():
    attempts = []

    def accepted_then_dropped(request: httpx.Request) -> httpx.Response:
        attempts.append("accepted")
        raise httpx.ReadError("response dropped", request=request)

    upstream = httpx.AsyncClient(
        base_url="https://api.anthropic.com",
        transport=httpx.MockTransport(accepted_then_dropped),
    )
    routes._upstream_client = upstream

    try:
        with pytest.raises(httpx.ReadError):
            await routes._upstream_send(_request(), b"{}", Settings())
        assert attempts == ["accepted"], "a possibly accepted request was replayed"
    finally:
        routes._upstream_client = None
        await upstream.aclose()




class _BlockedStreamResponse:
    status_code = 200

    async def aread(self):
        return b""

    async def aiter_lines(self):
        yield 'data: {"choices": []}'
        yield "data: [DONE]"


class _BlockedStreamContext:
    def __init__(self, owner):
        self.owner = owner

    async def __aenter__(self):
        self.owner.calls += 1
        self.owner.first_entered.set()
        await self.owner.release.wait()
        return _BlockedStreamResponse()

    async def __aexit__(self, *exc):
        return False


class _BlockedLocalStreamClient:
    def __init__(self):
        self.calls = 0
        self.first_entered = asyncio.Event()
        self.release = asyncio.Event()

    def stream(self, method, path, json):
        return _BlockedStreamContext(self)

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_local_provider_serializes_streams_instead_of_flooding_ollama():
    provider = ProviderClient(Settings(provider_base_url="http://127.0.0.1:11434/v1"))
    await provider._client.aclose()
    blocked = _BlockedLocalStreamClient()
    provider._client = blocked

    first_stream = provider.stream({"messages": []})
    second_stream = provider.stream({"messages": []})
    first = asyncio.create_task(anext(first_stream))
    await blocked.first_entered.wait()
    second = asyncio.create_task(anext(second_stream))
    await asyncio.sleep(0)

    assert blocked.calls == 1, "a retry opened another Ollama stream while one was running"

    blocked.release.set()
    assert await first == {"choices": []}
    await first_stream.aclose()
    assert await second == {"choices": []}
    await second_stream.aclose()


@pytest.mark.asyncio
async def test_connectivity_probe_runs_off_the_event_loop(monkeypatch):
    seen_thread = []

    async def unavailable(request, body, settings):
        raise httpx.ConnectTimeout("timed out", request=httpx.Request("POST", "https://api.anthropic.com"))

    def offline_probe():
        seen_thread.append(threading.current_thread())
        return False

    routes._breaker = routes.FailoverBreaker(threshold=1, online_fn=offline_probe)
    monkeypatch.setattr(routes, "_upstream_send", unavailable)

    try:
        assert await routes._try_upstream(_request(), b"{}", Settings()) is None
        assert len(seen_thread) == 1
        assert seen_thread[0] is not threading.main_thread()
    finally:
        routes._breaker = None


@pytest.mark.asyncio
async def test_success_cannot_be_overwritten_by_an_older_failure_probe(monkeypatch):
    probe_started = threading.Event()
    release_probe = threading.Event()
    success_sent = asyncio.Event()

    def offline_probe():
        probe_started.set()
        assert release_probe.wait(timeout=1)
        return False

    async def fail_or_succeed(request, body, settings):
        if body == b"fail":
            raise httpx.ConnectTimeout(
                "timed out",
                request=httpx.Request("POST", "https://api.anthropic.com"),
            )
        success_sent.set()
        return httpx.Response(
            200,
            content=b"ok",
            request=httpx.Request("POST", "https://api.anthropic.com"),
        )

    monkeypatch.setattr(routes, "_breaker_failure_lock", asyncio.Lock())
    routes._breaker = routes.FailoverBreaker(threshold=1, online_fn=offline_probe)
    monkeypatch.setattr(routes, "_upstream_send", fail_or_succeed)

    try:
        failure = asyncio.create_task(
            routes._try_upstream(_request(), b"fail", Settings())
        )
        assert await asyncio.to_thread(probe_started.wait, 1)

        success = asyncio.create_task(
            routes._try_upstream(_request(), b"success", Settings())
        )
        await success_sent.wait()
        await asyncio.sleep(0)
        release_probe.set()

        assert await failure is None
        response = await success
        assert response.status_code == 200
        assert routes._breaker.open is False
        assert routes._breaker.reason == ""
    finally:
        release_probe.set()
        routes._breaker = None


@pytest.mark.asyncio
async def test_cancelled_request_cannot_leave_a_stale_failure_probe(monkeypatch):
    probe_started = threading.Event()
    release_probe = threading.Event()
    probe_finished = threading.Event()

    def offline_probe():
        probe_started.set()
        assert release_probe.wait(timeout=1)
        probe_finished.set()
        return False

    monkeypatch.setattr(routes, "_breaker_failure_lock", asyncio.Lock())
    breaker = routes.FailoverBreaker(threshold=1, online_fn=offline_probe)
    failure = asyncio.create_task(routes._record_failure(breaker, "ConnectTimeout"))
    assert await asyncio.to_thread(probe_started.wait, 1)

    failure.cancel()
    success = asyncio.create_task(routes._record_success(breaker))
    await asyncio.sleep(0)
    release_probe.set()

    with pytest.raises(asyncio.CancelledError):
        await failure
    await success
    assert await asyncio.to_thread(probe_finished.wait, 1)
    assert breaker.open is False
    assert breaker.reason == ""
