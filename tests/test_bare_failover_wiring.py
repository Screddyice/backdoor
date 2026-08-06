"""End-to-end: a failed-over request reaches the local model already stripped.

test_bare.py covers `make_bare` as a pure function. This file covers the part
that actually broke in production — the wiring. The bug this repo just fixed was
never in the failover logic; it was that the failover logic sat behind a shell
function nobody noticed, so a correct breaker was simply never consulted. The
lesson generalizes: assert on what the local provider RECEIVES, not on what a
helper returns in isolation.
"""

import json

import httpx
import pytest

import src.proxy.routes as routes
from src.proxy.app import create_app
from src.proxy.config import Settings, get_settings
from src.proxy.failover import FailoverBreaker

HARNESS_SYSTEM = "You are Claude Code, Anthropic's official CLI. " * 2000


class RecordingClient:
    """Stands in for the local Ollama profile client and keeps the payload."""

    def __init__(self):
        self.payload = None

    async def complete(self, payload):
        self.payload = payload
        return {
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": "local answer", "tool_calls": []},
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }


def _offline_upstream() -> httpx.AsyncClient:
    """Every request fails at the transport layer, as it does with no network."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Network is unreachable")

    return httpx.AsyncClient(
        base_url="https://api.anthropic.com",
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture
def offline_app(monkeypatch):
    recorder = RecordingClient()
    routes._upstream_client = _offline_upstream()
    # threshold=1 so a single failure opens it; online_fn pinned False because
    # the breaker only opens when the HOST is offline, and a test must not
    # depend on the developer's actual connectivity.
    routes._breaker = FailoverBreaker(threshold=1, online_fn=lambda: False)
    monkeypatch.setattr(routes, "_get_profile_client", lambda profile, settings: recorder)

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        router_mode="hybrid", failover_to_local=True, failover_threshold=1
    )
    try:
        yield app, recorder
    finally:
        app.dependency_overrides.clear()
        routes._upstream_client = None
        routes._breaker = None


async def _post(app, body: dict) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/v1/messages",
            content=json.dumps(body).encode(),
            headers={"content-type": "application/json"},
        )


def _harness_request() -> dict:
    return {
        "model": "claude-opus-5",
        "system": HARNESS_SYSTEM,
        "tools": [
            {"name": "Bash", "description": "run a command", "input_schema": {}},
            {"name": "Read", "description": "read a file", "input_schema": {}},
            {"name": "mcp__plugin_mem0_mem0__search_memories",
             "description": "recall", "input_schema": {}},
        ],
        "messages": [
            {"role": "user", "content": "did the router ever fail over?"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"path": "log"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "Z" * 80_000},
            ]},
        ],
    }


async def test_failed_over_request_arrives_stripped(offline_app):
    app, recorder = offline_app
    resp = await _post(app, _harness_request())

    assert resp.status_code == 200
    assert recorder.payload is not None, "request never reached the local model"

    sent = json.dumps(recorder.payload)
    # The harness system prompt must not be in what the local model receives.
    assert "official CLI" not in sent
    # Nor the 80KB tool result.
    assert "Z" * 5000 not in sent
    # No tool definitions at all: deepseek-r1 rejects a request carrying them
    # ("does not support tools", HTTP 400), which would kill the very session
    # failover exists to save.
    assert not recorder.payload.get("tools"), recorder.payload.get("tools")


async def test_the_users_actual_question_survives(offline_app):
    """Stripping is only correct if the session still makes sense afterwards."""
    app, recorder = offline_app
    await _post(app, _harness_request())
    assert "did the router ever fail over?" in json.dumps(recorder.payload)


async def test_bare_mode_can_be_disabled(offline_app, monkeypatch):
    """An escape hatch that does not work is not an escape hatch. If bare mode
    ever strips something load-bearing, `failover_bare=false` has to restore the
    old behavior without a code change."""
    app, recorder = offline_app
    app.dependency_overrides[get_settings] = lambda: Settings(
        router_mode="hybrid", failover_to_local=True, failover_threshold=1,
        failover_bare=False,
    )
    await _post(app, _harness_request())
    assert "official CLI" in json.dumps(recorder.payload)


async def test_stripped_size_picks_the_tier(offline_app):
    """The regression guard for the ordering bug this change had to avoid:
    sizing on the RAW body would route a bare-able session to the wide 4B tier
    and waste the stronger model entirely."""
    from src.proxy.config import pick_failover_profile
    from src.proxy.bare import make_bare
    from src.proxy.models import MessagesRequest
    from src.proxy.tokens import count_messages

    req = MessagesRequest.model_validate(_harness_request())
    raw = count_messages(req.messages, req.system, req.tools)
    bare = make_bare(req)
    stripped = count_messages(bare.messages, bare.system, bare.tools)

    assert pick_failover_profile(raw) == "local-failover-256k"       # the 4B
    assert pick_failover_profile(stripped) == "local-failover-deepseek"
