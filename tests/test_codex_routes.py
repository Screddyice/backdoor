import json
import logging
from pathlib import Path

import httpx
import pytest

from src.proxy import codex_routes
from src.proxy.app import create_app
from src.proxy.config import Settings, get_settings
from src.proxy.failover import FailoverBreaker


FIXTURE = Path(__file__).parent / "fixtures" / "codex_responses_request.json"
SSE = (
    b'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_local"}}\n\n'
    b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp_local","status":"completed"}}\n\n'
)


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, data: bytes):
        self.data = data

    async def __aiter__(self):
        yield self.data

    async def aclose(self):
        return None


class FailingStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"event: response.created\ndata: {}\n\n"
        raise httpx.ReadError("stream failed")

    async def aclose(self):
        return None


@pytest.fixture
async def codex_app(monkeypatch, tmp_path):
    settings = Settings(
        codex_chatgpt_upstream="https://chatgpt.test/backend-api/codex",
        codex_failover_threshold=3,
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    breaker = FailoverBreaker(
        threshold=3,
        notify_fn=lambda *_: None,
        online_fn=lambda: True,
        state_path=tmp_path / "codex-state.json",
        source="codex",
        upstream_name="ChatGPT Codex",
        require_offline=False,
    )
    monkeypatch.setattr(codex_routes, "_codex_breaker", breaker)
    monkeypatch.setattr(codex_routes, "_chatgpt_client", None)
    monkeypatch.setattr(codex_routes, "_ollama_client", None)
    yield app, settings, breaker
    for client_name in ("_chatgpt_client", "_ollama_client"):
        client = getattr(codex_routes, client_name, None)
        if client is not None:
            await client.aclose()


@pytest.mark.asyncio
async def test_online_codex_request_relays_original_body_headers_and_sse(
    codex_app, monkeypatch
):
    app, settings, _ = codex_app
    body = FIXTURE.read_bytes()
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            stream=BytesStream(SSE),
            headers={"content-type": "text/event-stream", "x-request-id": "req-test"},
        )

    monkeypatch.setattr(
        codex_routes,
        "_chatgpt_client",
        httpx.AsyncClient(
            base_url=settings.codex_chatgpt_upstream,
            transport=httpx.MockTransport(upstream),
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://backdoor.test"
    ) as client:
        response = await client.post(
            "/backend-api/codex/responses",
            content=body,
            headers={
                "content-type": "application/json",
                "authorization": "Bearer auth-marker",
                "chatgpt-account-id": "account-marker",
                "session_id": "session-marker",
                "originator": "codex_cli_rs",
                "connection": "close",
            },
        )

    assert response.status_code == 200
    assert response.content == SSE
    assert response.headers["x-request-id"] == "req-test"
    assert len(seen) == 1
    assert seen[0].url == httpx.URL("https://chatgpt.test/backend-api/codex/responses")
    assert seen[0].content == body
    assert seen[0].headers["authorization"] == "Bearer auth-marker"
    assert seen[0].headers["chatgpt-account-id"] == "account-marker"
    assert seen[0].headers["session_id"] == "session-marker"
    assert seen[0].headers["originator"] == "codex_cli_rs"
    assert seen[0].headers.get("connection") != "close"
    assert seen[0].headers["host"] == "chatgpt.test"


@pytest.mark.asyncio
async def test_codex_route_rejects_over_budget_before_contacting_cloud(
    codex_app, monkeypatch
):
    app, settings, _ = codex_app
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["input"][4]["content"][0]["text"] = "oversized " * 100_000
    calls = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=SSE)

    monkeypatch.setattr(
        codex_routes,
        "_chatgpt_client",
        httpx.AsyncClient(
            base_url=settings.codex_chatgpt_upstream,
            transport=httpx.MockTransport(upstream),
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://backdoor.test"
    ) as client:
        response = await client.post(
            "/backend-api/codex/responses",
            json=payload,
            headers={"authorization": "Bearer auth-marker"},
        )

    assert response.status_code == 413
    assert calls == 0


@pytest.mark.asyncio
async def test_codex_relay_logs_metadata_without_headers_or_body(
    codex_app, monkeypatch, caplog
):
    app, settings, _ = codex_app

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=BytesStream(SSE),
            headers={"content-type": "text/event-stream"},
        )

    monkeypatch.setattr(
        codex_routes,
        "_chatgpt_client",
        httpx.AsyncClient(
            base_url=settings.codex_chatgpt_upstream,
            transport=httpx.MockTransport(upstream),
        ),
    )
    with caplog.at_level(logging.INFO):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://backdoor.test"
        ) as client:
            response = await client.post(
                "/backend-api/codex/responses",
                content=FIXTURE.read_bytes(),
                headers={"authorization": "Bearer auth-log-marker"},
            )

    assert response.status_code == 200
    assert "auth-log-marker" not in caplog.text
    assert "active task" not in caplog.text
    assert "older task" not in caplog.text


@pytest.mark.asyncio
async def test_midstream_cloud_failure_arms_codex_breaker(codex_app, monkeypatch):
    app, settings, breaker = codex_app

    def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=FailingStream(),
            headers={"content-type": "text/event-stream"},
        )

    monkeypatch.setattr(
        codex_routes,
        "_chatgpt_client",
        httpx.AsyncClient(
            base_url=settings.codex_chatgpt_upstream,
            transport=httpx.MockTransport(upstream),
        ),
    )
    with pytest.raises((httpx.TransportError, ExceptionGroup)):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://backdoor.test"
        ) as client:
            await client.post(
                "/backend-api/codex/responses",
                content=FIXTURE.read_bytes(),
            )

    assert breaker.reason == "ReadError"
