import asyncio
import json
import logging
from pathlib import Path

import httpx
import pytest

from src.proxy import codex_routes, compute_lease
from src.proxy.app import create_app
from src.proxy.config import Settings, get_settings
from src.proxy.failover import FailoverBreaker
from src.proxy.tokens import count_text


FIXTURE = Path(__file__).parent / "fixtures" / "codex_responses_request.json"
SSE = (
    b'event: response.created\ndata: {"type":"response.created","response":{"id":"resp_local"}}\n\n'
    b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp_local","status":"completed"}}\n\n'
)
LOCAL_REASONING_SSE = (
    b'event: response.output_item.added\ndata: {"type":"response.output_item.added","output_index":0,"item":{"id":"rs_local","type":"reasoning","summary":[],"encrypted_content":"plain local reasoning"}}\n\n'
    b'event: response.content_part.added\ndata: {"type":"response.content_part.added","item_id":"rs_local","output_index":0,"content_index":0,"part":{"type":"reasoning_text","text":"plain local content part"}}\n\n'
    b'event: response.reasoning_summary_text.delta\ndata: {"type":"response.reasoning_summary_text.delta","item_id":"rs_local","output_index":0,"summary_index":0,"delta":"plain local reasoning"}\n\n'
    b'event: response.content_part.done\ndata: {"type":"response.content_part.done","item_id":"rs_local","output_index":0,"content_index":0,"part":{"type":"reasoning_text","text":"plain local content part"}}\n\n'
    b'event: response.output_item.done\ndata: {"type":"response.output_item.done","output_index":0,"item":{"id":"rs_local","type":"reasoning","summary":[{"type":"summary_text","text":"plain local reasoning"}],"encrypted_content":"plain local reasoning"}}\n\n'
    b'event: response.output_item.added\ndata: {"type":"response.output_item.added","output_index":1,"item":{"id":"fc_local","type":"function_call","call_id":"call_local","name":"read_file","arguments":"{}"}}\n\n'
    b'event: response.function_call_arguments.done\ndata: {"type":"response.function_call_arguments.done","item_id":"fc_local","output_index":1,"arguments":"{}"}\n\n'
    b'event: response.completed\ndata: {"type":"response.completed","response":{"id":"resp_local","status":"completed","output":[{"id":"rs_local","type":"reasoning","summary":[{"type":"summary_text","text":"plain local reasoning"}],"encrypted_content":"plain local reasoning"},{"id":"fc_local","type":"function_call","call_id":"call_local","name":"read_file","arguments":"{}"}]}}\n\n'
)


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, data: bytes):
        self.data = data

    async def __aiter__(self):
        yield self.data

    async def aclose(self):
        return None


class ChunkedStream(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

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


@pytest.mark.asyncio
async def test_codex_compaction_request_stays_on_chatgpt_relay(codex_app, monkeypatch):
    app, settings, _ = codex_app
    body = b'{"model":"gpt-5.6-sol","input":[]}'
    seen = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            stream=BytesStream(b'{"output_text":"compacted"}'),
            headers={"content-type": "application/json"},
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
            "/backend-api/codex/responses/compact?source=client",
            content=body,
            headers={"authorization": "Bearer auth-marker"},
        )

    assert response.status_code == 200
    assert response.json() == {"output_text": "compacted"}
    assert seen[0].url == httpx.URL(
        "https://chatgpt.test/backend-api/codex/responses/compact?source=client"
    )
    assert seen[0].content == body
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
    monkeypatch.setattr(codex_routes, "_local_state_lock", asyncio.Lock())

    async def no_external_recall(_payload, _settings):
        return []

    monkeypatch.setattr(
        codex_routes, "recall_codex_external_context", no_external_recall
    )
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
async def test_online_codex_request_does_not_build_a_local_payload(
    codex_app, monkeypatch
):
    app, settings, _ = codex_app

    def cloud(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=BytesStream(SSE))

    def should_not_decode(*_args, **_kwargs):
        raise AssertionError("healthy cloud traffic must stay byte-faithful")

    monkeypatch.setattr(codex_routes, "decode_codex_body", should_not_decode)
    monkeypatch.setattr(
        codex_routes,
        "_chatgpt_client",
        httpx.AsyncClient(
            base_url=settings.codex_chatgpt_upstream,
            transport=httpx.MockTransport(cloud),
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://backdoor.test"
    ) as client:
        response = await client.post(
            "/backend-api/codex/responses", content=FIXTURE.read_bytes()
        )

    assert response.status_code == 200
    assert response.content == SSE


@pytest.mark.asyncio
async def test_codex_route_rejects_an_oversized_encoded_body_before_cloud(
    codex_app, monkeypatch
):
    app, settings, _ = codex_app
    settings.codex_max_request_bytes = 128
    cloud_calls = 0

    def cloud(_request: httpx.Request) -> httpx.Response:
        nonlocal cloud_calls
        cloud_calls += 1
        return httpx.Response(200, stream=BytesStream(SSE))

    monkeypatch.setattr(
        codex_routes,
        "_chatgpt_client",
        httpx.AsyncClient(
            base_url=settings.codex_chatgpt_upstream,
            transport=httpx.MockTransport(cloud),
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://backdoor.test"
    ) as client:
        response = await client.post(
            "/backend-api/codex/responses",
            content=json.dumps({"input": [], "padding": "A" * 256}).encode(),
        )

    assert response.status_code == 413
    assert cloud_calls == 0


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

    external_memory = "external " * 50
    settings.codex_memory_budget_tokens = count_text(external_memory.strip())

    async def recall_external(_payload, _settings):
        return [external_memory]

    monkeypatch.setattr(codex_routes, "recall_context", recall)
    monkeypatch.setattr(
        codex_routes, "recall_codex_external_context", recall_external
    )
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
async def test_disabling_local_failover_always_relays_trigger_statuses(
    codex_app, monkeypatch, tmp_path
):
    app, settings, _ = codex_app
    settings.codex_failover_to_local = False
    breaker = one_shot_breaker(tmp_path)
    monkeypatch.setattr(codex_routes, "_codex_breaker", breaker)
    cloud_calls = 0
    local_calls = 0

    def cloud(_request: httpx.Request) -> httpx.Response:
        nonlocal cloud_calls
        cloud_calls += 1
        return httpx.Response(
            503,
            stream=BytesStream(b'{"error":"cloud unavailable"}'),
            headers={"content-type": "application/json"},
        )

    def local(_request: httpx.Request) -> httpx.Response:
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

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://backdoor.test"
    ) as client:
        first = await client.post(
            "/backend-api/codex/responses", content=FIXTURE.read_bytes()
        )
        second = await client.post(
            "/backend-api/codex/responses", content=FIXTURE.read_bytes()
        )

    assert first.status_code == 503
    assert second.status_code == 503
    assert cloud_calls == 2
    assert local_calls == 0
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
async def test_non_streaming_local_response_drops_reasoning_and_returns_json(
    codex_app, monkeypatch, tmp_path
):
    app, settings, _ = codex_app
    monkeypatch.setattr(codex_routes, "_codex_breaker", one_shot_breaker(tmp_path))
    request_payload = json.loads(FIXTURE.read_bytes())
    request_payload["stream"] = False
    local_payloads = []
    local_responses = []

    def cloud(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "offline"})

    def local(request: httpx.Request) -> httpx.Response:
        local_payloads.append(json.loads(request.content))
        response = httpx.Response(
            200,
            stream=BytesStream(
                json.dumps(
                    {
                        "id": "resp_local",
                        "object": "response",
                        "output": [
                            {
                                "id": "rs_local",
                                "type": "reasoning",
                                "encrypted_content": "plain local reasoning",
                            },
                            {
                                "id": "msg_local",
                                "type": "message",
                                "content": [
                                    {"type": "output_text", "text": "visible answer"}
                                ],
                            },
                        ],
                        "metadata": {"encrypted_content": "plain local metadata"},
                    }
                ).encode()
            ),
            headers={"content-type": "application/json"},
        )
        local_responses.append(response)
        return response

    async def no_recall(_query, _settings):
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
            "/backend-api/codex/responses",
            content=json.dumps(request_payload).encode(),
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert local_payloads[0]["stream"] is False
    assert response.json()["output"] == [
        {
            "id": "msg_local",
            "type": "message",
            "content": [{"type": "output_text", "text": "visible answer"}],
        }
    ]
    assert "encrypted_content" not in response.text
    assert "plain local reasoning" not in response.text
    assert local_responses[0].is_closed
    assert codex_routes._local_inflight == 0


@pytest.mark.asyncio
async def test_non_streaming_local_body_read_failure_returns_clean_502(
    codex_app, monkeypatch, tmp_path
):
    app, settings, _ = codex_app
    monkeypatch.setattr(codex_routes, "_codex_breaker", one_shot_breaker(tmp_path))
    request_payload = json.loads(FIXTURE.read_bytes())
    request_payload["stream"] = False
    local_responses = []

    def cloud(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "offline"})

    def local(_request: httpx.Request) -> httpx.Response:
        response = httpx.Response(
            200,
            stream=FailingStream(),
            headers={"content-type": "application/json"},
        )
        local_responses.append(response)
        return response

    async def no_recall(_query, _settings):
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
            "/backend-api/codex/responses",
            content=json.dumps(request_payload).encode(),
        )

    assert response.status_code == 502
    assert response.json() == {"detail": "Local Qwen unavailable"}
    assert local_responses[0].is_closed
    assert codex_routes._local_inflight == 0


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wire",
    [LOCAL_REASONING_SSE, LOCAL_REASONING_SSE.replace(b"\n", b"\r\n")],
)
async def test_local_stream_drops_reasoning_items_that_cloud_cannot_verify(
    codex_app, tmp_path, wire
):
    breaker = one_shot_breaker(tmp_path)
    response = httpx.Response(
        200,
        stream=ChunkedStream(
            *(wire[index : index + 1] for index in range(len(wire)))
        ),
    )
    stream = codex_routes._local_body(response, breaker)

    rendered = b"".join([chunk async for chunk in stream])

    assert b'"type":"reasoning"' not in rendered
    assert b"encrypted_content" not in rendered
    assert b"plain local reasoning" not in rendered
    assert b"plain local content part" not in rendered
    assert b"rs_local" not in rendered
    assert b'"type":"function_call"' in rendered
    assert b'"call_id":"call_local"' in rendered
    assert rendered.count(b'"output_index":0') == 2
    assert b'"output_index":1' not in rendered
    assert b"\n\n\n" not in rendered


def test_local_sse_sanitizer_reindexes_multiple_reasoning_items():
    dropped_indices = set()

    def frame(event_type, output_index, item_type):
        payload = {
            "type": event_type,
            "output_index": output_index,
            "item": {"type": item_type},
        }
        return b"data: " + json.dumps(payload).encode() + b"\n\n"

    assert (
        codex_routes._sanitize_local_sse_frame(
            frame("response.output_item.added", 0, "message"),
            dropped_indices,
            set(),
        )
        != b""
    )
    assert (
        codex_routes._sanitize_local_sse_frame(
            frame("response.output_item.added", 1, "reasoning"),
            dropped_indices,
            set(),
        )
        == b""
    )
    middle = codex_routes._sanitize_local_sse_frame(
        frame("response.output_item.added", 2, "function_call"),
        dropped_indices,
        set(),
    )
    assert b'"output_index":1' in middle
    assert (
        codex_routes._sanitize_local_sse_frame(
            frame("response.output_item.added", 3, "reasoning"),
            dropped_indices,
            set(),
        )
        == b""
    )
    final = codex_routes._sanitize_local_sse_frame(
        frame("response.output_item.added", 4, "message"),
        dropped_indices,
        set(),
    )
    assert b'"output_index":2' in final


@pytest.mark.parametrize("item_type", ["reasoning_summary", "reasoning_text"])
def test_local_sse_sanitizer_drops_reasoning_type_variants(item_type):
    payload = {
        "type": "response.output_item.added",
        "output_index": 0,
        "item": {"id": "rs_variant", "type": item_type},
    }
    frame = b"data: " + json.dumps(payload).encode() + b"\n\n"
    dropped_indices = set()
    dropped_item_ids = set()

    assert (
        codex_routes._sanitize_local_sse_frame(
            frame, dropped_indices, dropped_item_ids
        )
        == b""
    )
    assert dropped_indices == {0}
    assert dropped_item_ids == {"rs_variant"}


def test_local_sse_sanitizer_preserves_guard_frames_byte_for_byte():
    dropped_indices = set()
    dropped_item_ids = set()
    for frame in (b"data: [DONE]\n\n", b"data: []\n\n"):
        assert (
            codex_routes._sanitize_local_sse_frame(
                frame, dropped_indices, dropped_item_ids
            )
            == frame
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [b"{broken}", b"9" * 5_000],
)
async def test_local_stream_closes_on_unparseable_sse_json(
    codex_app, tmp_path, payload
):
    breaker = one_shot_breaker(tmp_path)
    response = httpx.Response(
        200,
        stream=ChunkedStream(b"data: " + payload + b"\n\n"),
    )
    stream = codex_routes._local_body(response, breaker)

    with pytest.raises(ValueError, match="unparseable SSE frame"):
        await anext(stream)

    assert response.is_closed
    assert codex_routes._local_inflight == 0


@pytest.mark.asyncio
async def test_local_stream_closes_when_the_json_decoder_recurses(
    codex_app, monkeypatch, tmp_path
):
    breaker = one_shot_breaker(tmp_path)
    response = httpx.Response(200, stream=ChunkedStream(b"data: {}\n\n"))
    stream = codex_routes._local_body(response, breaker)

    def recurse(_payload):
        raise RecursionError

    monkeypatch.setattr(codex_routes.json, "loads", recurse)
    with pytest.raises(ValueError, match="unparseable SSE frame"):
        await anext(stream)

    assert response.is_closed
    assert codex_routes._local_inflight == 0


@pytest.mark.asyncio
async def test_local_stream_rejects_an_oversized_sse_frame(
    codex_app, monkeypatch, tmp_path
):
    breaker = one_shot_breaker(tmp_path)
    monkeypatch.setattr(codex_routes, "_MAX_LOCAL_SSE_FRAME_BYTES", 16)
    response = httpx.Response(200, stream=ChunkedStream(b"data: ", b"x" * 11))
    stream = codex_routes._local_body(response, breaker)

    with pytest.raises(ValueError, match="size limit"):
        await anext(stream)

    assert response.is_closed
    assert codex_routes._local_inflight == 0


@pytest.mark.asyncio
async def test_local_stream_discards_an_incomplete_trailing_frame(
    codex_app, tmp_path
):
    breaker = one_shot_breaker(tmp_path)
    response = httpx.Response(
        200,
        stream=ChunkedStream(b'data: {"type":"response.reasoning_summary_text.delta"}'),
    )
    stream = codex_routes._local_body(response, breaker)

    assert b"".join([chunk async for chunk in stream]) == b""
    assert response.is_closed
    assert codex_routes._local_inflight == 0


@pytest.mark.asyncio
async def test_unstarted_local_stream_close_releases_response_and_slot(
    codex_app, monkeypatch, tmp_path
):
    _, settings, _ = codex_app
    breaker = one_shot_breaker(tmp_path)
    assert breaker.record_failure("HTTP 503") is True
    breaker.note_claim("http://127.0.0.1:11434/v1", settings.codex_local_model)
    response = httpx.Response(200, stream=BytesStream(SSE))
    stream = codex_routes._local_body(response, breaker)

    assert codex_routes._local_inflight == 1
    breaker.record_success()
    await codex_routes._release_claims(breaker)
    await stream.aclose()

    assert response.is_closed
    assert codex_routes._local_inflight == 0


@pytest.mark.asyncio
async def test_local_client_disconnect_closes_response_and_releases_slot(
    codex_app, tmp_path
):
    breaker = one_shot_breaker(tmp_path)
    response = httpx.Response(200, stream=BytesStream(SSE))
    stream = codex_routes._local_body(response, breaker)
    relay = codex_routes._ManagedStreamingResponse(stream, status_code=200)

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            raise OSError("client disconnected")

    with pytest.raises(Exception):
        await relay(
            {
                "type": "http",
                "method": "POST",
                "path": "/backend-api/codex/responses",
                "headers": [],
                "asgi": {"version": "3.0", "spec_version": "2.4"},
            },
            receive,
            send,
        )

    assert response.is_closed
    assert codex_routes._local_inflight == 0


@pytest.mark.asyncio
async def test_qwen_unload_blocks_a_new_local_reservation(
    codex_app, monkeypatch, tmp_path
):
    _, settings, _ = codex_app
    breaker = one_shot_breaker(tmp_path)
    assert breaker.record_failure("HTTP 503") is True
    breaker.note_claim("http://127.0.0.1:11434/v1", settings.codex_local_model)
    breaker.record_success()
    unload_started = asyncio.Event()
    allow_unload = asyncio.Event()

    async def unload(_base_url, _model):
        unload_started.set()
        await allow_unload.wait()
        return True

    monkeypatch.setattr(codex_routes.ollama_admin, "unload", unload)
    release_task = asyncio.create_task(codex_routes._release_claims(breaker))
    await unload_started.wait()
    reserve_task = asyncio.create_task(codex_routes._reserve_local_slot())
    await asyncio.sleep(0)

    assert reserve_task.done() is False
    allow_unload.set()
    await release_task
    await reserve_task

    assert codex_routes._local_inflight == 1
    await codex_routes._release_local_slot(breaker)


@pytest.mark.asyncio
async def test_local_request_reserves_qwen_before_its_stream_is_iterated(
    codex_app, monkeypatch, tmp_path
):
    _, settings, _ = codex_app
    breaker = one_shot_breaker(tmp_path)
    assert breaker.record_failure("HTTP 503") is True
    unloaded = []

    async def resolve(_profile):
        return "local-qwen38-obliterated"

    async def prepare(payload, _settings):
        return payload

    async def no_external_recall(_payload, _settings):
        return []

    async def no_recall(_query, _settings):
        return []

    async def send_local(_payload, _settings):
        return httpx.Response(200, stream=BytesStream(SSE))

    async def unload(base_url, model):
        unloaded.append((base_url, model))
        return True

    monkeypatch.setattr(codex_routes.mlx_admin, "resolve_profile", resolve)
    monkeypatch.setattr(codex_routes, "prepare_codex_external_context", prepare)
    monkeypatch.setattr(
        codex_routes, "recall_codex_external_context", no_external_recall
    )
    monkeypatch.setattr(codex_routes, "recall_context", no_recall)
    monkeypatch.setattr(codex_routes, "_send_local", send_local)
    monkeypatch.setattr(codex_routes.ollama_admin, "unload", unload)
    monkeypatch.setattr(
        compute_lease, "claim_exclusive_model", lambda *_args, **_kwargs: None
    )

    response = await codex_routes._serve_local(
        json.loads(FIXTURE.read_text(encoding="utf-8")),
        settings,
        breaker,
        "race-test",
        0.0,
    )

    assert codex_routes._local_inflight == 1
    breaker.record_success()
    await codex_routes._release_claims(breaker)
    assert unloaded == []

    stream = response.body_iterator
    assert await anext(stream) == SSE
    await stream.aclose()
    assert unloaded == [
        ("http://127.0.0.1:11434/v1", "qwen3.8:27b-obliterated")
    ]


@pytest.mark.asyncio
async def test_local_route_skips_all_cognee_recall_when_disabled(
    codex_app, monkeypatch, tmp_path
):
    _, settings, _ = codex_app
    settings.qwen_cognee = False
    breaker = one_shot_breaker(tmp_path)
    assert breaker.record_failure("HTTP 503") is True

    async def resolve(_profile):
        return "local-qwen38-obliterated"

    async def prepare(payload, _settings):
        return payload

    async def forbidden_recall(*_args):
        raise AssertionError("Cognee recall must stay off")

    async def send_local(_payload, _settings):
        return httpx.Response(200, stream=BytesStream(SSE))

    monkeypatch.setattr(codex_routes.mlx_admin, "resolve_profile", resolve)
    monkeypatch.setattr(codex_routes, "prepare_codex_external_context", prepare)
    monkeypatch.setattr(
        codex_routes, "recall_codex_external_context", forbidden_recall
    )
    monkeypatch.setattr(codex_routes, "recall_context", forbidden_recall)
    monkeypatch.setattr(codex_routes, "_send_local", send_local)
    monkeypatch.setattr(
        compute_lease, "claim_exclusive_model", lambda *_args, **_kwargs: None
    )

    response = await codex_routes._serve_local(
        json.loads(FIXTURE.read_text(encoding="utf-8")),
        settings,
        breaker,
        "offline-test",
        0.0,
    )
    await response.body_iterator.aclose()


@pytest.mark.asyncio
async def test_oversized_local_instruction_is_rejected_before_cognee(
    codex_app, monkeypatch, tmp_path
):
    _, settings, _ = codex_app
    breaker = one_shot_breaker(tmp_path)
    assert breaker.record_failure("HTTP 503") is True
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["input"][4]["content"][0]["text"] = "oversized " * 30_000
    calls = []

    async def resolve(_profile):
        return "local-qwen38-obliterated"

    async def forbidden(*_args):
        calls.append("called")
        raise AssertionError("oversized instructions must not reach Cognee")

    monkeypatch.setattr(codex_routes.mlx_admin, "resolve_profile", resolve)
    monkeypatch.setattr(codex_routes, "prepare_codex_external_context", forbidden)
    monkeypatch.setattr(codex_routes, "recall_codex_external_context", forbidden)
    monkeypatch.setattr(codex_routes, "recall_context", forbidden)

    with pytest.raises(Exception) as caught:
        await codex_routes._serve_local(
            payload,
            settings,
            breaker,
            "oversized-test",
            0.0,
        )

    assert getattr(caught.value, "status_code", None) == 413
    assert calls == []


class HangUpStream(httpx.AsyncByteStream):
    """Cloud body that dies from something other than a transport error.

    The gap this covers: `_cloud_body` only recorded success in its `else`
    branch and only recorded failure for `httpx.TransportError`. Anything
    else — a client disconnect, a bug, a cancellation — fell between the two
    and the probe's outcome was discarded.
    """

    async def __aiter__(self):
        yield b'event: response.created\ndata: {"type":"response.created"}\n\n'
        raise RuntimeError("client hung up")

    async def aclose(self):
        return None


def _half_open_codex_breaker(tmp_path, now):
    """An OPEN codex breaker holding a qwen claim, with its probe window due."""
    breaker = one_shot_breaker(tmp_path, now_fn=lambda: now["value"])
    assert breaker.record_failure("ConnectError") is True
    breaker.note_claim("http://127.0.0.1:11434/v1", "qwen3.8:27b-obliterated")
    now["value"] += 61
    return breaker


@pytest.mark.asyncio
async def test_half_open_probe_closes_breaker_when_the_stream_never_completes(
    codex_app, monkeypatch, tmp_path
):
    """A probe that reached ChatGPT must close the breaker even if the body dies.

    Regression for 2026-08-30: a blip opened the codex breaker at 23:09:48 and
    it was still open 19 minutes later, having logged `path=cloud status=200`
    twice at 23:13:46 and 23:15:23. Both probes reached ChatGPT; neither closed
    the breaker, because success was credited only after a fully relayed
    stream. Codex answered from a local 27B for 309s and 385s while the cloud
    was fine.
    """
    app, settings, _ = codex_app
    now = {"value": 1_000.0}
    breaker = _half_open_codex_breaker(tmp_path, now)
    monkeypatch.setattr(codex_routes, "_codex_breaker", breaker)

    def cloud(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=HangUpStream(),
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

    with pytest.raises((RuntimeError, ExceptionGroup)):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://backdoor.test"
        ) as client:
            await client.post(
                "/backend-api/codex/responses", content=FIXTURE.read_bytes()
            )

    # Reachability was proven by the status line, so the next Codex turn goes to
    # the cloud instead of waiting out another probe interval on a local 27B.
    assert breaker.open is False
    # The tier stays claimed: this stream did not finish, so the retry that
    # follows may still need it. Unloading now would only buy a reload.
    assert unloaded == []


@pytest.mark.asyncio
async def test_half_open_probe_closes_breaker_when_the_client_disconnects(
    codex_app, monkeypatch, tmp_path
):
    """The real 2026-08-30 shape: the client hangs up while the body streams.

    Driven at the ASGI layer on purpose. httpx's ASGITransport always runs the
    app to completion, so it cannot express a disconnect — and a disconnect is
    precisely the case that used to be lost, since closing the generator raises
    GeneratorExit at the `yield` and runs neither `except` nor `else`.
    """
    app, settings, _ = codex_app
    now = {"value": 1_000.0}
    breaker = _half_open_codex_breaker(tmp_path, now)
    monkeypatch.setattr(codex_routes, "_codex_breaker", breaker)

    def cloud(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=BytesStream(SSE.replace(b"resp_local", b"resp_cloud")),
            headers={"content-type": "text/event-stream"},
        )

    monkeypatch.setattr(
        codex_routes,
        "_chatgpt_client",
        httpx.AsyncClient(
            base_url=settings.codex_chatgpt_upstream,
            transport=httpx.MockTransport(cloud),
        ),
    )

    payload = FIXTURE.read_bytes()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/backend-api/codex/responses",
        "raw_path": b"/backend-api/codex/responses",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"backdoor.test"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
        ],
        "client": ("127.0.0.1", 51234),
        "server": ("backdoor.test", 80),
    }

    # Starlette watches for a disconnect by polling `receive`, so it has to
    # block after the request body rather than answer in a loop.
    delivered = False
    still_connected = asyncio.Event()

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await still_connected.wait()  # never set: the poll just parks here
        return {"type": "http.disconnect"}

    started = []

    async def send(message):
        if message["type"] == "http.response.start":
            started.append(message["status"])
            return
        # First byte of the body is where the client goes away.
        raise RuntimeError("client disconnected")

    with pytest.raises((RuntimeError, ExceptionGroup)):
        await app(scope, receive, send)

    assert started == [200]
    assert breaker.open is False
