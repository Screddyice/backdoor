"""End-to-end wiring for bounded, read-only outage inference."""

import asyncio
import json
import sqlite3

import httpx
import pytest

import src.proxy.routes as routes
from src.proxy.app import create_app
from src.proxy.client import ProviderError
from src.proxy.config import Settings, get_settings
from src.proxy.context_tokenizer import TokenCount
from src.proxy.failover import FailoverBreaker


CLOUD_BODY = b'{"cloud":true}'


class CloudStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield CLOUD_BODY

    async def aclose(self):
        pass


class SwitchingTransport(httpx.AsyncBaseTransport):
    def __init__(self, offline: bool = True):
        self.offline = offline
        self.calls = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.offline:
            raise httpx.ConnectError("Network is unreachable", request=request)
        return httpx.Response(
            200,
            stream=CloudStream(),
            headers={"content-type": "application/json"},
            request=request,
        )


class RecordingClient:
    def __init__(self):
        self.payloads = []
        self.delay = 0.0
        self.failure: ProviderError | None = None
        self.stream_calls = 0
        self.stream_delay_before = 0.0
        self.stream_delay_after = 0.0

    async def complete(self, payload):
        self.payloads.append(payload)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failure is not None:
            raise self.failure
        return {
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": "local answer", "tool_calls": []},
            }],
            "usage": {"prompt_tokens": 180, "completion_tokens": 2},
        }

    async def stream(self, payload):
        self.payloads.append(payload)
        self.stream_calls += 1
        if self.stream_delay_before:
            await asyncio.sleep(self.stream_delay_before)
        yield {
            "choices": [{"delta": {"content": "local "}, "finish_reason": None}],
        }
        if self.stream_delay_after:
            await asyncio.sleep(self.stream_delay_after)
        yield {
            "choices": [{"delta": {"content": "answer"}, "finish_reason": None}],
        }
        yield {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 180, "completion_tokens": 2},
        }


class RecordingGate:
    def __init__(self, fits: bool = True):
        self.allowed = fits
        self.calls = []

    def fits(self, payload, hard_limit):
        self.calls.append((payload, hard_limit))
        return self.allowed, TokenCount(
            value=hard_limit if self.allowed else hard_limit + 1,
            source="llama-tokenize",
            exact=True,
        )


def long_request(*, stream: bool = False) -> dict:
    messages = [
        {"role": "user", "content": "The rollback revision was 621d765."},
        {"role": "assistant", "content": "recorded"},
    ]
    for index in range(70):
        messages.extend([
            {"role": "user", "content": f"old filler question {index} " * 100},
            {"role": "assistant", "content": f"old filler answer {index} " * 100},
        ])
    messages.append({"role": "user", "content": "Which rollback revision did we record?"})
    return {
        "model": "claude-opus-5",
        "system": "Claude harness " * 500,
        "stream": stream,
        "max_tokens": 8_192,
        "tools": [
            {"name": "Read", "description": "read", "input_schema": {}},
            {"name": "Glob", "description": "glob", "input_schema": {}},
            {"name": "Grep", "description": "grep", "input_schema": {}},
            {"name": "Bash", "description": "shell", "input_schema": {}},
            {"name": "Edit", "description": "edit", "input_schema": {}},
            {"name": "mcp__remote__lookup", "description": "remote", "input_schema": {}},
        ],
        "messages": messages,
    }


async def post(app, body: dict) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.post(
            "/v1/messages",
            content=json.dumps(body).encode(),
            headers={"content-type": "application/json"},
        )


@pytest.fixture
async def virtualized_app(tmp_path, monkeypatch):
    transport = SwitchingTransport()
    upstream = httpx.AsyncClient(
        base_url="https://api.anthropic.com",
        transport=transport,
    )
    recorder = RecordingClient()
    gate = RecordingGate()
    routes._upstream_client = upstream
    routes._breaker = FailoverBreaker(
        threshold=1,
        recovery_successes=2,
        online_fn=lambda: False,
    )
    routes._context_runtimes.clear()
    monkeypatch.setattr(routes, "_get_profile_client", lambda _profile, _settings: recorder)
    monkeypatch.setattr(routes, "_get_token_gate", lambda _settings: gate)

    real_load = routes.load_profile_settings

    def isolated_profile(profile):
        settings = real_load(profile).model_copy(deep=True)
        settings.memory_inject = False
        settings.qwen_cognee = False
        return settings

    monkeypatch.setattr(routes, "load_profile_settings", isolated_profile)
    settings = Settings(
        router_mode="hybrid",
        failover_to_local=True,
        failover_threshold=1,
        context_virtualization=True,
        context_store_path=str(tmp_path / "transcripts.sqlite3"),
        context_target_input_tokens=700,
        context_hard_input_tokens=900,
        context_retrieval_tokens=180,
        qwen_cognee=False,
        memory_inject=False,
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        yield app, recorder, gate, transport, settings
    finally:
        app.dependency_overrides.clear()
        for runtime in list(routes._context_runtimes.values()):
            await runtime.close()
        routes._context_runtimes.clear()
        routes._upstream_client = None
        routes._breaker = None
        await upstream.aclose()


async def test_long_outage_request_reaches_provider_bounded_and_read_only(virtualized_app):
    app, recorder, gate, _transport, settings = virtualized_app

    response = await post(app, long_request())

    assert response.status_code == 200
    assert len(recorder.payloads) == 1
    payload = recorder.payloads[0]
    assert payload["max_tokens"] == 1_024
    assert [tool["function"]["name"] for tool in payload.get("tools", [])] == [
        "Read",
        "Glob",
        "Grep",
    ]
    assert "Which rollback revision did we record?" in json.dumps(payload)
    assert "621d765" in json.dumps(payload)
    assert gate.calls and gate.calls[0][1] == settings.context_hard_input_tokens

    runtime = next(iter(routes._context_runtimes.values()))
    with sqlite3.connect(runtime.store.path) as connection:
        assert connection.execute("SELECT count(*) FROM lineages").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM lineage_segments").fetchone()[0] > 100


async def test_healthy_cloud_body_is_unchanged_and_archived(virtualized_app):
    app, recorder, _gate, transport, _settings = virtualized_app
    transport.offline = False

    response = await post(app, long_request())
    runtime = next(iter(routes._context_runtimes.values()))
    await runtime._archive_queue.join()

    assert response.content == CLOUD_BODY
    assert recorder.payloads == []
    with sqlite3.connect(runtime.store.path) as connection:
        assert connection.execute("SELECT count(*) FROM lineages").fetchone()[0] == 1


async def test_cloud_archive_is_deferred_to_response_background():
    calls = []

    async def close_upstream():
        calls.append("closed")

    class Runtime:
        def archive_cloud(self, request):
            calls.append(("archived", request.model))

    request = routes.MessagesRequest(
        model="claude-opus-5",
        messages=[{"role": "user", "content": "keep this turn"}],
    )
    response = routes.StreamingResponse(
        iter([b"cloud"]),
        background=routes.BackgroundTask(close_upstream),
    )

    routes._schedule_cloud_archive(response, Runtime(), request)

    assert calls == []
    assert response.background is not None
    await response.background()
    assert calls == ["closed", ("archived", "claude-opus-5")]


async def test_codex_named_cloud_request_never_uses_local_provider(virtualized_app):
    app, recorder, _gate, transport, _settings = virtualized_app
    transport.offline = False
    body = long_request()
    body["model"] = "gpt-5.6-codex"

    response = await post(app, body)

    assert response.content == CLOUD_BODY
    assert recorder.payloads == []
    assert routes._context_runtimes == {}


async def test_current_instruction_over_limit_returns_continuity_without_provider(virtualized_app):
    app, recorder, _gate, _transport, settings = virtualized_app
    settings.context_target_input_tokens = 50
    settings.context_hard_input_tokens = 60
    body = long_request()
    body["messages"][-1]["content"] = "x" * 20_000

    response = await post(app, body)

    assert response.status_code == 200
    assert "local inference could not finish" in response.text
    assert recorder.payloads == []


async def test_token_gate_refusal_returns_continuity_without_provider(virtualized_app):
    app, recorder, gate, _transport, _settings = virtualized_app
    gate.allowed = False

    response = await post(app, long_request())

    assert response.status_code == 200
    assert "local inference could not finish" in response.text
    assert recorder.payloads == []


async def test_provider_failure_returns_continuity(virtualized_app):
    app, recorder, _gate, _transport, _settings = virtualized_app
    recorder.failure = ProviderError(503, "model unavailable")

    response = await post(app, long_request())

    assert response.status_code == 200
    assert "local inference could not finish" in response.text


async def test_store_failure_returns_continuity_without_provider(virtualized_app, monkeypatch):
    app, recorder, _gate, _transport, _settings = virtualized_app

    def fail_store(_settings):
        raise OSError("database corrupt")

    routes._context_runtimes.clear()
    monkeypatch.setattr(routes, "_get_context_runtime", fail_store)

    response = await post(app, long_request())

    assert response.status_code == 200
    assert "local inference could not finish" in response.text
    assert recorder.payloads == []


async def test_completed_local_retry_wins_after_connectivity_recovers(virtualized_app):
    app, recorder, _gate, transport, _settings = virtualized_app
    body = long_request()

    first = await post(app, body)
    upstream_calls = transport.calls
    transport.offline = False
    second = await post(app, body)

    assert second.content == first.content
    assert len(recorder.payloads) == 1
    assert transport.calls == upstream_calls


async def test_stream_retry_reuses_completed_events_after_recovery(virtualized_app):
    app, recorder, _gate, transport, _settings = virtualized_app
    body = long_request(stream=True)

    first = await post(app, body)
    upstream_calls = transport.calls
    transport.offline = False
    second = await post(app, body)

    assert first.status_code == 200
    assert "event: message_stop" in first.text
    assert second.content == first.content
    assert recorder.stream_calls == 1
    assert transport.calls == upstream_calls


async def test_stream_without_first_text_returns_continuity_before_deadline(virtualized_app):
    app, recorder, _gate, _transport, settings = virtualized_app
    settings.failover_first_text_seconds = 0.02
    settings.failover_total_seconds = 0.2
    recorder.stream_delay_before = 0.08

    response = await post(app, long_request(stream=True))

    assert response.status_code == 200
    assert "local inference could not finish" in response.text
    assert "event: message_stop" in response.text


async def test_stream_total_deadline_emits_marker_and_terminal_event(virtualized_app):
    app, recorder, _gate, _transport, settings = virtualized_app
    settings.failover_first_text_seconds = 0.05
    settings.failover_total_seconds = 0.02
    recorder.stream_delay_after = 0.08

    response = await post(app, long_request(stream=True))

    assert response.status_code == 200
    assert "local " in response.text
    assert "truncated during the outage" in response.text
    terminal = response.text.rstrip().split("\n\n")[-1]
    assert terminal.splitlines()[0] == "event: message_stop"
    assert json.loads(terminal.splitlines()[1][5:].strip()) == {"type": "message_stop"}
