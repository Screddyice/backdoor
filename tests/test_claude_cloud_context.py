"""Cloud Claude traffic must never inherit a local Qwen context limit."""

import json

import httpx
import pytest

import src.proxy.routes as routes
from src.proxy.app import create_app
from src.proxy.config import Settings, get_settings


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, data: bytes):
        self.data = data

    async def __aiter__(self):
        yield self.data


@pytest.mark.asyncio
async def test_claude_cloud_request_above_local_budget_stays_byte_faithful(
    monkeypatch,
    tmp_path,
):
    payload = {
        "model": "claude-opus-5",
        "max_tokens": 16,
        "stream": True,
        "messages": [{"role": "user", "content": "token " * 30_000}],
    }
    body = json.dumps(payload, separators=(",", ":")).encode()
    seen: list[httpx.Request] = []
    cloud_sse = b'event: message_stop\ndata: {"type":"message_stop"}\n\n'

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            stream=BytesStream(cloud_sse),
            headers={"content-type": "text/event-stream"},
        )

    def local_path_must_not_run(*_args, **_kwargs):
        raise AssertionError("healthy Claude cloud traffic entered local Qwen sizing")

    upstream_client = httpx.AsyncClient(
        base_url="https://api.anthropic.com",
        transport=httpx.MockTransport(upstream),
    )
    monkeypatch.setattr(routes, "_upstream_client", upstream_client)
    monkeypatch.setattr(routes, "_breaker", None)
    monkeypatch.setattr(routes, "prepare_external_context", local_path_must_not_run)
    monkeypatch.setattr(routes, "count_messages", local_path_must_not_run)
    monkeypatch.setattr(routes, "load_profile_settings", local_path_must_not_run)

    settings = Settings(
        _env_file=None,
        router_mode="hybrid",
        failover_to_local=True,
        route_max_input_tokens=28_000,
        context_virtualization=True,
        context_archive_path=str(tmp_path / "context" / "transcripts.sqlite3"),
        context_tokenizer_executable="/missing/llama-tokenize",
        context_tokenizer_model_path="/missing/model.gguf",
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://backdoor.test"
        ) as client:
            response = await client.post(
                "/v1/messages",
                content=body,
                headers={"content-type": "application/json"},
            )
    finally:
        app.dependency_overrides.clear()
        await upstream_client.aclose()
        routes._upstream_client = None
        routes._breaker = None

    assert response.status_code == 200
    assert response.content == cloud_sse
    assert len(seen) == 1
    assert seen[0].content == body
