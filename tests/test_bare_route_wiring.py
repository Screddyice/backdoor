"""End-to-end: an explicit `/model qwen` reaches the local model already stripped.

test_bare_failover_wiring.py covers the same guarantee for the FAILOVER path.
This file exists because those are two different branches, and only one of them
stripped. `MODEL_ROUTES.get(model)` returning a profile skips the failover block
entirely, so routing `qwen` at the 27B tier — whose window is 32K precisely
because bare mode keeps prompts small — handed it a full harness session and
overflowed it. The failover logic was correct the whole time; it was simply not
on this path. Same lesson as the sibling file: assert on what the local provider
RECEIVES, not on what a helper returns in isolation.
"""

import json

import httpx
import pytest

import src.proxy.routes as routes
from src.proxy.app import create_app
from src.proxy.config import MODEL_ROUTES, Settings, get_settings, load_profile_settings

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


@pytest.fixture
def routed_app(monkeypatch):
    """Hybrid router, upstream never consulted: a MODEL_ROUTES hit goes local.

    No offline transport here on purpose. If a change ever makes this path fall
    through to failover, the missing upstream client surfaces it loudly instead
    of the test quietly passing for the wrong reason.
    """
    recorder = RecordingClient()
    monkeypatch.setattr(routes, "_get_profile_client", lambda profile, settings: recorder)

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(router_mode="hybrid")
    try:
        yield app, recorder, monkeypatch
    finally:
        app.dependency_overrides.clear()


async def _post(app, body: dict) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/v1/messages",
            content=json.dumps(body).encode(),
            headers={"content-type": "application/json"},
        )


def _harness_request(model: str = "qwen") -> dict:
    return {
        "model": model,
        "system": HARNESS_SYSTEM,
        "tools": [
            {"name": "Bash", "description": "run a command", "input_schema": {}},
            {"name": "Read", "description": "read a file", "input_schema": {}},
            {"name": "WebSearch", "description": "search the web", "input_schema": {}},
            {"name": "WebFetch", "description": "fetch a URL", "input_schema": {}},
            {"name": "mcp__example__crm_lookup",
             "description": "lookup", "input_schema": {}},
        ],
        "messages": [
            {"role": "user", "content": "why is qwen still the 4b?"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"path": "cfg"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "Z" * 80_000},
            ]},
        ],
    }


def _pin(monkeypatch, **overrides):
    """Pin the profile settings the route resolves to, independent of disk."""
    base = dict(
        router_mode="hybrid",
        provider_base_url="http://localhost:11434/v1",
        provider_model="qwen3.8:27b-obliterated",
        qwen_memory=False,
    )
    monkeypatch.setattr(
        routes, "load_profile_settings", lambda profile: Settings(**base, **overrides)
    )


async def test_explicit_route_arrives_stripped(routed_app):
    app, recorder, monkeypatch = routed_app
    _pin(monkeypatch, route_bare=True)

    resp = await _post(app, _harness_request())

    assert resp.status_code == 200
    assert recorder.payload is not None, "request never reached the local model"

    sent = json.dumps(recorder.payload)
    assert "official CLI" not in sent, "harness system prompt survived the strip"
    assert "Z" * 5000 not in sent, "80KB tool result survived the strip"

    names = [t.get("function", {}).get("name", "") for t in (recorder.payload.get("tools") or [])]
    assert {"Bash", "Read", "WebSearch", "WebFetch"}.issubset(names), names
    assert not any(n.startswith("mcp__") for n in names), names
    assert "When current information would improve the answer" in sent
    assert "lost its network connection" not in sent


async def test_explicit_route_externalizes_large_fetched_page(routed_app):
    """The GUI may vary; the provider request crossing Backdoor is the seam."""
    app, recorder, monkeypatch = routed_app
    _pin(monkeypatch, route_bare=True)
    body = _harness_request()
    body["messages"][1]["content"][0]["name"] = "WebFetch"
    body["messages"][1]["content"][0]["input"] = {"url": "https://example.com/report"}
    body["messages"].append({"role": "user", "content": "What does the report say?"})

    resp = await _post(app, body)

    assert resp.status_code == 200
    sent = json.dumps(recorder.payload)
    assert "<qwen-external-context" in sent
    assert "https://example.com/report" in sent
    assert "Z" * 10_000 not in sent


async def test_the_users_actual_question_survives(routed_app):
    """Stripping is only correct if the session still makes sense afterwards."""
    app, recorder, monkeypatch = routed_app
    _pin(monkeypatch, route_bare=True)
    await _post(app, _harness_request())
    assert "why is qwen still the 4b?" in json.dumps(recorder.payload)


async def test_route_without_route_bare_is_left_alone(routed_app):
    """The 64K `qwen-fast` tier must NOT be stripped.

    Blanket-stripping every MODEL_ROUTES hit would silently delete the system
    prompt and MCP tools out from under callers that selected the full profile.
    """
    app, recorder, monkeypatch = routed_app
    _pin(monkeypatch, route_bare=False)
    await _post(app, _harness_request())
    assert "official CLI" in json.dumps(recorder.payload)


async def test_stripping_failure_does_not_drop_the_request(routed_app):
    """An unstripped answer beats no answer — same rule as the failover path."""
    app, recorder, monkeypatch = routed_app
    _pin(monkeypatch, route_bare=True)

    def boom(*_a, **_k):
        raise RuntimeError("strip exploded")

    monkeypatch.setattr(routes, "make_bare", boom)

    resp = await _post(app, _harness_request())
    assert resp.status_code == 200
    assert recorder.payload is not None, "a stripping failure swallowed the request"


def test_qwen_route_targets_a_profile_that_declares_route_bare():
    """The wiring guard: the flag has to be ON the profile `qwen` resolves to.

    Reads the real profile off disk, because every part of this can be correct
    in isolation while the one file that matters is missing the setting — which
    is exactly how this shipped broken.
    """
    profile = MODEL_ROUTES["qwen"]
    settings = load_profile_settings(profile)
    assert settings.route_bare is True, (
        f"MODEL_ROUTES['qwen'] -> {profile}, which does not set ROUTE_BARE. "
        "A full harness session will overflow its 32K window."
    )


# --- the replacement system prompt --------------------------------------
#
# Stripping is only half the job: something has to stand in for the prompt that
# was removed. The route path shipped using the FAILOVER text, which told a
# perfectly healthy session it had "lost its network connection" — and took the
# operator rules with it, because make_bare replaces `system` wholesale and the
# qwen wrapper's `--append-system-prompt` copy lived there.


async def test_route_is_not_told_it_is_in_an_outage(routed_app):
    """A `/model qwen` switch is a choice, not a failure. Saying otherwise makes
    the model hedge and decline work it can actually do."""
    app, recorder, monkeypatch = routed_app
    _pin(monkeypatch, route_bare=True, route_system_file="")

    await _post(app, _harness_request())

    sent = json.dumps(recorder.payload)
    assert "lost its network connection" not in sent, "route path got the FAILOVER prompt"
    assert "chosen on purpose" in sent, sent[:400]


async def test_operator_rules_survive_the_strip(routed_app, tmp_path):
    """The rules the wrapper injects must reach the model on this path too."""
    app, recorder, monkeypatch = routed_app
    rules = tmp_path / "rules.md"
    rules.write_text("EVERY BRANCH GETS A PR.\n", encoding="utf-8")
    _pin(monkeypatch, route_bare=True, route_system_file=str(rules))

    await _post(app, _harness_request())

    assert "EVERY BRANCH GETS A PR." in json.dumps(recorder.payload)


async def test_missing_rules_file_still_routes(routed_app, tmp_path):
    """A documentation file must never be able to fail a request."""
    app, recorder, monkeypatch = routed_app
    _pin(monkeypatch, route_bare=True, route_system_file=str(tmp_path / "nope.md"))

    resp = await _post(app, _harness_request())

    assert resp.status_code == 200
    assert "chosen on purpose" in json.dumps(recorder.payload)


def test_failover_keeps_the_outage_prompt():
    """The route change must not leak into failover, where the text IS true."""
    from src.proxy.bare import OFFLINE_SYSTEM, ROUTE_SYSTEM, make_bare
    from src.proxy.models import MessagesRequest

    req = MessagesRequest.model_validate({
        "model": "claude-opus-5",
        "system": "harness",
        "messages": [{"role": "user", "content": "hi"}],
    })

    # The failover branch in routes.py passes OFFLINE_SYSTEM explicitly; the
    # route path passes its own. Neither text leaks into the other.
    assert make_bare(req, system=OFFLINE_SYSTEM).system == OFFLINE_SYSTEM
    assert "lost its network connection" in OFFLINE_SYSTEM
    assert "lost its network connection" not in ROUTE_SYSTEM


# ── Profile mode: the direct path the wrapper is supposed to guard ───────────
#
# `bd switch ...; bd claude` runs the router in profile mode, which bypasses
# MODEL_ROUTES entirely — so the hybrid branch above never executes and nothing
# had honored the profile's bare contract. The `qwen` wrapper launches Claude
# Code with --bare, but a caller who skips the wrapper reached the local tier
# with the full harness attached. Ported from PR #61.


@pytest.fixture
def profile_app(monkeypatch):
    """Profile mode: every request translates to the single active profile."""
    recorder = RecordingClient()
    monkeypatch.setattr(routes, "get_provider_client", lambda: recorder)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        router_mode="profile",
        route_bare=True,
        provider_base_url="http://localhost:11434/v1",
        provider_model="qwen3.8:27b-obliterated",
        qwen_memory=False,
    )
    try:
        yield app, recorder
    finally:
        app.dependency_overrides.clear()


async def test_profile_mode_strips_the_harness_when_route_bare_is_on(profile_app):
    app, recorder = profile_app
    resp = await _post(app, _harness_request(model="claude-opus-4-1"))
    assert resp.status_code == 200

    sent = json.dumps(recorder.payload)
    assert HARNESS_SYSTEM[:200] not in sent, (
        "profile mode bypasses MODEL_ROUTES, so nothing else strips this path"
    )
    assert len(sent) < 80_000, "the 80KB tool result must have been truncated too"


async def test_profile_mode_keeps_the_operator_rules_it_strips(profile_app):
    """The strip replaces the whole system prompt, so the rules must ride along.

    This is the half PR #61 could not have had: it predates `route_system`, and
    porting its plain make_bare call verbatim would have reintroduced the exact
    bug that work fixed on the hybrid path — the session losing rules it is still
    expected to follow because stripping deleted the only copy.
    """
    app, recorder = profile_app
    resp = await _post(app, _harness_request(model="claude-opus-4-1"))
    assert resp.status_code == 200

    sent = json.dumps(recorder.payload)
    assert "Nothing has failed and the network is up" in sent, (
        "the replacement prompt must be ROUTE_SYSTEM: nothing failed on this path"
    )
    assert "lost its network connection" not in sent, (
        "OFFLINE_SYSTEM here would tell a perfectly online model it is offline"
    )


async def test_profile_mode_leaves_the_harness_alone_when_route_bare_is_off(monkeypatch):
    """route_bare is opt-in per profile; a wide tier keeps its harness."""
    recorder = RecordingClient()
    monkeypatch.setattr(routes, "get_provider_client", lambda: recorder)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        router_mode="profile",
        route_bare=False,
        provider_base_url="http://localhost:11434/v1",
        provider_model="qwen3.8:27b-obliterated",
        qwen_memory=False,
    )
    try:
        resp = await _post(app, _harness_request(model="claude-opus-4-1"))
        assert resp.status_code == 200
        assert HARNESS_SYSTEM[:200] in json.dumps(recorder.payload)
    finally:
        app.dependency_overrides.clear()
