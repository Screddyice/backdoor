"""Claude Messages virtualization after Backdoor selects local Qwen."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import src.proxy.routes as routes
from src.proxy.app import create_app
from src.proxy.config import Settings, get_settings
from src.proxy.failover import FailoverBreaker


class RecordingProvider:
    def __init__(self) -> None:
        self.last_payload: dict | None = None
        self.calls = 0

    async def complete(self, payload: dict) -> dict:
        self.calls += 1
        self.last_payload = payload
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": "local answer", "tool_calls": []},
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }


def _offline_upstream() -> httpx.AsyncClient:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Network is unreachable")

    return httpx.AsyncClient(
        base_url="https://api.anthropic.com",
        transport=httpx.MockTransport(handler),
    )


def claude_payload_with_history(model: str) -> dict:
    messages = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"history-{index} " + ("old " * 20_000),
        }
        for index in range(7)
    ]
    messages.append({"role": "user", "content": "current-user-marker"})
    return {
        "model": model,
        "max_tokens": 32,
        "tools": [
            {"name": name, "description": name, "input_schema": {}}
            for name in ("Read", "Glob", "Grep", "Bash", "Edit")
        ],
        "messages": messages,
    }


def single_instruction(model: str) -> dict:
    return {
        "model": model,
        "max_tokens": 32,
        "messages": [
            {
                "role": "user",
                "content": ("current-instruction " * 23_000).strip(),
            }
        ],
    }


def _provider_size(payload: dict | None) -> int:
    assert payload is not None
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode())


@pytest.fixture
def local_route(tmp_path, monkeypatch):
    provider = RecordingProvider()
    original_load_profile = routes.load_profile_settings

    def load_profile(profile: str) -> Settings:
        return original_load_profile(profile).model_copy(
            update={
                "context_virtualization": True,
                "context_archive_path": str(tmp_path / "context" / "transcripts.sqlite3"),
                "context_tokenizer_executable": "/missing/llama-tokenize",
                "context_tokenizer_model_path": "/missing/model.gguf",
                "memory_inject": False,
                "qwen_cognee": False,
            }
        )

    async def keep_profile(profile: str) -> str:
        return profile

    async def no_keep_alive(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(routes, "load_profile_settings", load_profile)
    monkeypatch.setattr(routes, "_get_profile_client", lambda _profile, _settings: provider)
    monkeypatch.setattr(routes.mlx_admin, "resolve_profile", keep_profile)
    monkeypatch.setattr(routes.ollama_admin, "set_keep_alive", no_keep_alive)
    monkeypatch.setattr(routes.compute_lease, "claim_exclusive_model", lambda *_a, **_k: None)

    settings = Settings(
        _env_file=None,
        router_mode="hybrid",
        failover_to_local=True,
        failover_threshold=1,
        failover_min_outage_seconds=0,
        context_virtualization=True,
        context_archive_path=str(tmp_path / "context" / "transcripts.sqlite3"),
        context_tokenizer_executable="/missing/llama-tokenize",
        context_tokenizer_model_path="/missing/model.gguf",
        qwen_cognee=False,
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        yield app, provider
    finally:
        app.dependency_overrides.clear()
        routes._upstream_client = None
        routes._breaker = None


async def _post(app, payload: dict) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://backdoor.test"
    ) as client:
        return await client.post("/v1/messages", json=payload)


@pytest.mark.asyncio
async def test_deliberate_qwen_switch_compacts_142k_claude_history(local_route):
    app, provider = local_route

    response = await _post(app, claude_payload_with_history("qwen"))

    assert response.status_code == 200
    assert _provider_size(provider.last_payload) <= 22_000
    assert "current-user-marker" in json.dumps(provider.last_payload)


@pytest.mark.asyncio
async def test_claude_breaker_uses_same_compactor(local_route):
    app, provider = local_route
    routes._upstream_client = _offline_upstream()
    routes._breaker = FailoverBreaker(
        threshold=1,
        min_outage=0,
        online_fn=lambda: False,
    )

    response = await _post(app, claude_payload_with_history("claude-opus-5"))

    assert response.status_code == 200
    assert _provider_size(provider.last_payload) <= 22_000
    tools = provider.last_payload.get("tools", []) if provider.last_payload else []
    assert {
        tool["function"]["name"] for tool in tools
    } <= {"Read", "Glob", "Grep"}


@pytest.mark.asyncio
async def test_deliberate_qwen_returns_413_when_current_instruction_cannot_fit(
    local_route,
):
    app, _provider = local_route

    response = await _post(app, single_instruction("qwen"))

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_outage_returns_continuity_message_when_compaction_cannot_fit(
    local_route,
):
    app, _provider = local_route
    routes._upstream_client = _offline_upstream()
    routes._breaker = FailoverBreaker(
        threshold=1,
        min_outage=0,
        online_fn=lambda: False,
    )

    response = await _post(app, single_instruction("claude-opus-5"))

    assert response.status_code == 200
    assert "local inference could not fit this turn" in response.text


@pytest.mark.asyncio
async def test_identical_deliberate_qwen_retries_share_one_generation(local_route):
    app, provider = local_route
    payload = claude_payload_with_history("qwen")

    left, right = await asyncio.gather(_post(app, payload), _post(app, payload))

    assert left.status_code == right.status_code == 200
    assert provider.calls == 1
