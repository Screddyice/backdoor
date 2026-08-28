import json
import logging
from pathlib import Path

import httpx
import pytest

from src.proxy import codex_routes, compute_lease
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


@pytest.mark.asyncio
async def test_codex_models_probe_is_relayed_for_cli_and_desktop_doctor(
    codex_app, monkeypatch
):
    app, settings, _ = codex_app
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(401, json={"error": "auth required"})

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
        response = await client.get(
            "/backend-api/codex/models",
            headers={"authorization": "Bearer auth-marker"},
        )

    assert response.status_code == 401
    assert seen[0].url == httpx.URL("https://chatgpt.test/backend-api/codex/models")
    assert seen[0].headers["authorization"] == "Bearer auth-marker"


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
    monkeypatch.setattr(codex_routes, "_local_inflight", 0)
    monkeypatch.setattr(codex_routes, "_deferred_claims", set())
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
async def test_codex_route_relays_request_above_local_budget_to_cloud(
    codex_app, monkeypatch
):
    app, settings, _ = codex_app
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["input"][4]["content"][0]["text"] = "token " * 28_000
    body = json.dumps(payload).encode()
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
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
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://backdoor.test"
    ) as client:
        response = await client.post(
            "/backend-api/codex/responses",
            content=body,
            headers={"authorization": "Bearer auth-marker"},
        )

    assert response.status_code == 200
    assert response.content == SSE
    assert len(seen) == 1
    assert seen[0].content == body


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


def one_shot_breaker(tmp_path, now_fn=lambda: 1_000.0):
    return FailoverBreaker(
        threshold=1,
        probe_interval=60.0,
        now_fn=now_fn,
        notify_fn=lambda *_: None,
        online_fn=lambda: True,
        state_path=tmp_path / "one-shot-codex-state.json",
        source="codex",
        upstream_name="ChatGPT Codex",
        require_offline=False,
    )


@pytest.mark.asyncio
async def test_eligible_cloud_failure_uses_fresh_cognee_backed_qwen_request(
    codex_app, monkeypatch, tmp_path
):
    app, settings, _ = codex_app
    breaker = one_shot_breaker(tmp_path)
    monkeypatch.setattr(codex_routes, "_codex_breaker", breaker)
    claims = []
    monkeypatch.setattr(
        compute_lease,
        "claim_exclusive_model",
        lambda model, **kwargs: claims.append((model, kwargs)),
    )
    cloud_calls = []
    local_calls = []

    def cloud(request: httpx.Request) -> httpx.Response:
        cloud_calls.append(request)
        return httpx.Response(503, json={"error": "temporarily unavailable"})

    def local(request: httpx.Request) -> httpx.Response:
        local_calls.append(request)
        return httpx.Response(
            200,
            stream=BytesStream(SSE.replace(b"resp_local", b"resp_qwen")),
            headers={"content-type": "text/event-stream"},
        )

    async def recall(query, recall_settings):
        assert query == "active task"
        return ["Cognee continuity marker"]

    monkeypatch.setattr(codex_routes, "recall_context", recall)
    monkeypatch.setattr(
        codex_routes,
        "_chatgpt_client",
        httpx.AsyncClient(
            base_url=settings.codex_chatgpt_upstream,
            transport=httpx.MockTransport(cloud),
        ),
    )
    monkeypatch.setattr(
        codex_routes,
        "_ollama_client",
        httpx.AsyncClient(transport=httpx.MockTransport(local)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://backdoor.test"
    ) as client:
        response = await client.post(
            "/backend-api/codex/responses",
            content=FIXTURE.read_bytes(),
            headers={"authorization": "Bearer cloud-only-marker"},
        )

    assert response.status_code == 200
    assert b"resp_qwen" in response.content
    assert len(cloud_calls) == 1
    assert len(local_calls) == 1
    local_payload = json.loads(local_calls[0].content)
    rendered = json.dumps(local_payload)
    assert local_payload["model"] == "qwen3.8:27b-obliterated"
    assert "active task" in rendered
    assert "Cognee continuity marker" in rendered
    assert "bounded result" in rendered
    assert "older task" not in rendered
    assert "prompt_cache_key" not in rendered
    assert "reasoning" not in rendered
    assert "authorization" not in local_calls[0].headers
    assert "chatgpt-account-id" not in local_calls[0].headers
    assert breaker.open is True
    assert claims == [
        (
            "qwen3.8:27b-obliterated",
            {"source": "codex-failover", "ttl_seconds": 600},
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403])
async def test_auth_and_request_errors_never_activate_local_failover(
    codex_app, monkeypatch, tmp_path, status
):
    app, settings, _ = codex_app
    breaker = one_shot_breaker(tmp_path)
    monkeypatch.setattr(codex_routes, "_codex_breaker", breaker)
    local_calls = 0
    local_preparations = 0

    async def prepare_local(payload, _settings):
        nonlocal local_preparations
        local_preparations += 1
        return payload

    def cloud(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            stream=BytesStream(b'{"error":"visible"}'),
            headers={"content-type": "application/json"},
        )

    def local(request: httpx.Request) -> httpx.Response:
        nonlocal local_calls
        local_calls += 1
        return httpx.Response(200, stream=BytesStream(SSE))

    monkeypatch.setattr(
        codex_routes,
        "_chatgpt_client",
        httpx.AsyncClient(
            base_url=settings.codex_chatgpt_upstream,
            transport=httpx.MockTransport(cloud),
        ),
    )
    monkeypatch.setattr(
        codex_routes,
        "_ollama_client",
        httpx.AsyncClient(transport=httpx.MockTransport(local)),
    )
    monkeypatch.setattr(
        codex_routes, "prepare_codex_external_context", prepare_local
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://backdoor.test"
    ) as client:
        response = await client.post(
            "/backend-api/codex/responses", content=FIXTURE.read_bytes()
        )

    assert response.status_code == status
    assert response.json() == {"error": "visible"}
    assert local_calls == 0
    assert local_preparations == 0
    assert breaker.open is False


@pytest.mark.asyncio
async def test_empty_cognee_recall_still_serves_current_task_from_qwen(
    codex_app, monkeypatch, tmp_path
):
    app, settings, _ = codex_app
    monkeypatch.setattr(codex_routes, "_codex_breaker", one_shot_breaker(tmp_path))

    def cloud(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "offline"})

    local_payloads = []

    def local(request: httpx.Request) -> httpx.Response:
        local_payloads.append(json.loads(request.content))
        return httpx.Response(200, stream=BytesStream(SSE))

    async def no_recall(query, recall_settings):
        return []

    monkeypatch.setattr(codex_routes, "recall_context", no_recall)
    monkeypatch.setattr(
        codex_routes,
        "_chatgpt_client",
        httpx.AsyncClient(
            base_url=settings.codex_chatgpt_upstream,
            transport=httpx.MockTransport(cloud),
        ),
    )
    monkeypatch.setattr(
        codex_routes,
        "_ollama_client",
        httpx.AsyncClient(transport=httpx.MockTransport(local)),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://backdoor.test"
    ) as client:
        response = await client.post(
            "/backend-api/codex/responses", content=FIXTURE.read_bytes()
        )

    assert response.status_code == 200
    assert "active task" in json.dumps(local_payloads[0])
    assert "Relevant context recalled from local Cognee" not in json.dumps(local_payloads[0])


@pytest.mark.asyncio
async def test_half_open_success_returns_same_thread_to_cloud(
    codex_app, monkeypatch, tmp_path
):
    app, settings, _ = codex_app
    now = {"value": 1_000.0}
    breaker = one_shot_breaker(tmp_path, now_fn=lambda: now["value"])
    monkeypatch.setattr(codex_routes, "_codex_breaker", breaker)
    cloud_attempts = 0

    def cloud(request: httpx.Request) -> httpx.Response:
        nonlocal cloud_attempts
        cloud_attempts += 1
        if cloud_attempts == 1:
            return httpx.Response(503, json={"error": "offline"})
        return httpx.Response(
            200,
            stream=BytesStream(SSE.replace(b"resp_local", b"resp_cloud")),
            headers={"content-type": "text/event-stream"},
        )

    def local(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=BytesStream(SSE.replace(b"resp_local", b"resp_qwen")),
            headers={"content-type": "text/event-stream"},
        )

    async def no_recall(query, recall_settings):
        return []

    monkeypatch.setattr(codex_routes, "recall_context", no_recall)
    async def unload(*args):
        return True

    monkeypatch.setattr(codex_routes.ollama_admin, "unload", unload)
    monkeypatch.setattr(
        codex_routes,
        "_chatgpt_client",
        httpx.AsyncClient(
            base_url=settings.codex_chatgpt_upstream,
            transport=httpx.MockTransport(cloud),
        ),
    )
    monkeypatch.setattr(
        codex_routes,
        "_ollama_client",
        httpx.AsyncClient(transport=httpx.MockTransport(local)),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://backdoor.test"
    ) as client:
        local_response = await client.post(
            "/backend-api/codex/responses", content=FIXTURE.read_bytes()
        )
        now["value"] += 61
        cloud_response = await client.post(
            "/backend-api/codex/responses", content=FIXTURE.read_bytes()
        )

    assert b"resp_qwen" in local_response.content
    assert b"resp_cloud" in cloud_response.content
    assert breaker.open is False
    assert cloud_attempts == 2


@pytest.mark.asyncio
async def test_half_open_stream_failure_keeps_breaker_open_and_qwen_claimed(
    codex_app, monkeypatch, tmp_path
):
    app, settings, _ = codex_app
    now = {"value": 1_000.0}
    breaker = one_shot_breaker(tmp_path, now_fn=lambda: now["value"])
    assert breaker.record_failure("HTTP 503") is True
    breaker.note_claim("http://127.0.0.1:11434/v1", settings.codex_local_model)
    now["value"] += 61
    monkeypatch.setattr(codex_routes, "_codex_breaker", breaker)

    def cloud(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=FailingStream(),
            headers={"content-type": "text/event-stream"},
        )

    unloaded = []

    async def unload(base_url, model):
        unloaded.append((base_url, model))
        return True

    monkeypatch.setattr(codex_routes.ollama_admin, "unload", unload)
    monkeypatch.setattr(
        codex_routes,
        "_chatgpt_client",
        httpx.AsyncClient(
            base_url=settings.codex_chatgpt_upstream,
            transport=httpx.MockTransport(cloud),
        ),
    )

    with pytest.raises((httpx.TransportError, ExceptionGroup)):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://backdoor.test"
        ) as client:
            await client.post(
                "/backend-api/codex/responses", content=FIXTURE.read_bytes()
            )

    assert breaker.open is True
    assert unloaded == []


@pytest.mark.asyncio
async def test_ollama_rejection_returns_clean_502(codex_app, monkeypatch, tmp_path):
    app, settings, _ = codex_app
    monkeypatch.setattr(codex_routes, "_codex_breaker", one_shot_breaker(tmp_path))

    def cloud(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "offline"})

    def local(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            stream=BytesStream(b'{"error":"unsupported field"}'),
            headers={"content-type": "application/json"},
        )

    async def no_recall(query, recall_settings):
        return []

    monkeypatch.setattr(codex_routes, "recall_context", no_recall)
    monkeypatch.setattr(
        codex_routes,
        "_chatgpt_client",
        httpx.AsyncClient(
            base_url=settings.codex_chatgpt_upstream,
            transport=httpx.MockTransport(cloud),
        ),
    )
    monkeypatch.setattr(
        codex_routes,
        "_ollama_client",
        httpx.AsyncClient(transport=httpx.MockTransport(local)),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://backdoor.test"
    ) as client:
        response = await client.post(
            "/backend-api/codex/responses", content=FIXTURE.read_bytes()
        )

    assert response.status_code == 502
    assert response.json() == {"detail": "Local Qwen rejected the request"}


@pytest.mark.asyncio
async def test_recovery_defers_qwen_unload_until_local_stream_finishes(
    monkeypatch, tmp_path
):
    breaker = one_shot_breaker(tmp_path)
    assert breaker.record_failure("HTTP 503") is True
    breaker.note_claim("http://127.0.0.1:11434/v1", "qwen3.8:27b-obliterated")
    response = httpx.Response(200, stream=BytesStream(SSE))
    stream = codex_routes._local_body(response, breaker)
    first_chunk = await anext(stream)
    assert first_chunk == SSE

    unloaded = []

    async def unload(base_url, model):
        unloaded.append((base_url, model))
        return True

    monkeypatch.setattr(codex_routes.ollama_admin, "unload", unload)
    breaker.record_success()
    await codex_routes._release_claims(breaker)
    assert unloaded == []

    await stream.aclose()
    assert unloaded == [
        ("http://127.0.0.1:11434/v1", "qwen3.8:27b-obliterated")
    ]
