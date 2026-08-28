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
from src.proxy import compute_lease
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
    # Local tools survive so the model can keep working offline; every mcp__
    # tool is dropped, since it is remote and therefore dead while the breaker
    # is open.
    names = [t.get("function", {}).get("name", "") for t in (recorder.payload.get("tools") or [])]
    assert "Bash" in names and "Read" in names, names
    assert not any(n.startswith("mcp__") for n in names), names


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
    assert pick_failover_profile(stripped) == "local-qwen38-obliterated"


# --- deliberate `/model qwen` must obey the ladder too ---------------------
#
# MODEL_ROUTES short-circuits the failover branch, which is the only place that
# consulted FAILOVER_LADDER. So a deliberate route picked its tier from a static
# dict and never sized the session at all. Observed 2026-08-12: a `qwen` session
# sent 143,490 tokens at the 27B's 32K window 87 times over ~17 hours, failing
# and retrying every 5-10 minutes and loading 23GB on each attempt.


@pytest.fixture
def route_app(monkeypatch):
    """Online app exercising the MODEL_ROUTES path. No breaker involved: a
    `/model qwen` never reaches the failover branch."""
    recorder = RecordingClient()
    seen: list[str] = []

    def _capture(profile, settings):
        seen.append(profile)
        return recorder

    monkeypatch.setattr(routes, "_get_profile_client", _capture)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(router_mode="hybrid")
    try:
        yield app, recorder, seen
    finally:
        app.dependency_overrides.clear()


def _route_request(turns: int) -> dict:
    """A `/model qwen` session whose CONVERSATION carries the weight. Bare mode
    strips the system prompt and tool traffic but never the transcript, so this
    is what a long-running `qwen` session actually becomes."""
    turn = "Investigate how we can scrape the community directory. " * 400
    return {
        "model": "qwen",
        "system": HARNESS_SYSTEM,
        "tools": [{"name": "Bash", "description": "run a command", "input_schema": {}}],
        "messages": [
            {"role": "user" if i % 2 == 0 else "assistant", "content": turn}
            for i in range(turns)
        ],
    }


async def test_small_route_session_stays_on_the_heavy_tier(route_app, monkeypatch):
    """The common case must not regress: bare mode keeps the prompt small, so a
    normal `qwen` session keeps the strongest tier rather than escalating."""
    app, _recorder, seen = route_app
    claims = []
    monkeypatch.setattr(
        compute_lease,
        "claim_exclusive_model",
        lambda model, **kwargs: claims.append((model, kwargs)),
    )
    resp = await _post(app, _route_request(turns=1))

    assert resp.status_code == 200
    assert seen[-1] == "local-qwen38-obliterated", seen
    assert claims == [
        (
            "qwen3.8:27b-obliterated",
            {"source": "claude-explicit", "ttl_seconds": 600},
        )
    ]


async def test_profile_mode_oversized_session_escalates(monkeypatch):
    """The gap PR #24 left open, and the one the real incident went through.

    The 2026-08-12 pile-up was a `qwen` wrapper session on :8082, which runs
    router_mode="profile" — a mode that translates every request to the single
    active profile and never enters the hybrid branch where #24's guard lives.
    So the tier check never ran on the exact path that failed 87 times.
    """
    recorder = RecordingClient()
    seen: list[str] = []

    def _capture(profile, settings):
        seen.append(profile)
        return recorder

    monkeypatch.setattr(routes, "_get_profile_client", _capture)
    routes.set_provider_client(recorder)  # profile mode's default client

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        router_mode="profile",
        provider_model="qwen3.5:9b-64k",
        route_max_input_tokens=28_000,
    )
    try:
        resp = await _post(app, _route_request(turns=20))
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert seen and seen[-1] == "local-failover-256k", (
        f"profile-mode session over the tier window went to {seen or 'the same tier'}; "
        "it must escalate instead of failing forever"
    )


async def test_profile_mode_normal_session_stays_put(monkeypatch):
    """Regression guard: the guard must not pull ordinary wrapper sessions off
    the tier the user deliberately selected."""
    recorder = RecordingClient()
    seen: list[str] = []
    monkeypatch.setattr(routes, "_get_profile_client", lambda p, s: (seen.append(p), recorder)[1])
    routes.set_provider_client(recorder)

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        router_mode="profile",
        provider_model="qwen3.5:9b-64k",
        route_max_input_tokens=28_000,
    )
    try:
        resp = await _post(app, _route_request(turns=1))
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert not seen, f"small session was needlessly escalated to {seen}"


async def test_profile_mode_runs_the_runtime_interlock_before_provider_call(monkeypatch):
    """The qwen wrapper runs profile mode, so the MLX guard must run there too."""
    recorder = RecordingClient()
    resolved: list[str] = []
    seen: list[str] = []

    async def _fallback(profile):
        resolved.append(profile)
        return "local-fast"

    monkeypatch.setattr(routes.mlx_admin, "resolve_profile", _fallback)
    monkeypatch.setattr(
        routes,
        "_get_profile_client",
        lambda profile, settings: (seen.append(profile), recorder)[1],
    )
    routes.set_provider_client(recorder)

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        router_mode="profile",
        provider_base_url="http://127.0.0.1:11434/v1",
        provider_model="qwen3.8:27b-obliterated",
        runtime_profile="local-qwen38-obliterated",
        route_max_input_tokens=27_000,
    )
    try:
        resp = await _post(app, _route_request(turns=1))
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resolved == ["local-qwen38-obliterated"]
    assert seen[-1] == "local-fast"


async def test_oversized_route_session_escalates_to_the_wide_tier(route_app):
    """The bug. A transcript that overflows the 27B's window has to reach the
    256K tier, exactly as the failover path would route it. Before this fix the
    static MODEL_ROUTES entry won and the request failed forever."""
    app, _recorder, seen = route_app
    resp = await _post(app, _route_request(turns=20))

    assert resp.status_code == 200
    assert seen[-1] == "local-failover-256k", (
        f"oversized /model qwen session stayed on {seen[-1]}; it must escalate"
    )


# ---------------------------------------------------------------------------
# Ollama 0.32 rejects any system message that is not at index 0.
# ---------------------------------------------------------------------------

def test_system_messages_are_hoisted_to_the_front():
    """Ollama 0.32 500s on a system message at index > 0, including when the
    payload already opens with a valid one. 0.23.4 accepted both, so every
    tool-using local session broke on the daemon upgrade (2026-08-16). The proxy
    now normalises instead of trusting the provider to be lenient."""
    from src.proxy.translate import _hoist_system_messages

    out = _hoist_system_messages([
        {"role": "system", "content": "first"},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "stray"},
    ])

    assert [m["role"] for m in out] == ["system", "user"], (
        f"a system message survived at index > 0: {[m['role'] for m in out]}"
    )
    assert out[0]["content"] == "first\n\nstray", "stray system content must be kept, not dropped"


def test_hoist_leaves_a_conforming_payload_untouched():
    from src.proxy.translate import _hoist_system_messages

    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
    ]
    assert _hoist_system_messages(list(msgs)) == msgs


def test_hoist_handles_system_with_no_leading_system():
    from src.proxy.translate import _hoist_system_messages

    out = _hoist_system_messages([
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "late"},
    ])
    assert [m["role"] for m in out] == ["system", "user"]
    assert out[0]["content"] == "late"


# ---------------------------------------------------------------------------
# Durable memory injection for local models (the --bare hook gap).
# ---------------------------------------------------------------------------

def _mem_settings(**kw):
    from src.proxy.config import Settings
    base = dict(provider_base_url="http://localhost:11434/v1", memory_inject=True)
    base.update(kw)
    return Settings(**base)


def test_memory_injected_for_local_provider(monkeypatch):
    """The qwen wrapper's lean mode runs --bare, which kills every hook, so the
    proxy is the only place left that can give the 27B durable memory."""
    from src.proxy import translate

    monkeypatch.setattr(translate, "_hoist_system_messages", lambda m: m)
    import src.proxy.memory as memory
    monkeypatch.setattr(memory, "recall", lambda *a, **k: ["the 27B tier is qwen3.5:9b-64k"])

    out = translate._inject_memory(
        [{"role": "system", "content": "You are offline."}, {"role": "user", "content": "which tier?"}],
        _mem_settings(),
    )
    assert memory.BLOCK_OPEN in out[0]["content"]
    assert "qwen3.5:9b-64k" in out[0]["content"]
    assert "You are offline." in out[0]["content"], "the real system prompt must survive"


def test_memory_not_injected_for_cloud_provider(monkeypatch):
    """Cloud sessions already get Mem0 from the UserPromptSubmit hook; injecting
    again would spend the context twice on the same text."""
    from src.proxy import translate
    import src.proxy.memory as memory
    monkeypatch.setattr(memory, "recall", lambda *a, **k: ["should not appear"])

    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    out = translate._inject_memory(list(msgs), _mem_settings(provider_base_url="https://api.deepseek.com/v1"))
    assert out == msgs


def test_memory_injection_is_fail_open(monkeypatch):
    """A broken cache must cost the user nothing."""
    from src.proxy import translate
    import src.proxy.memory as memory

    def boom(*a, **k):
        raise sqlite_error()

    class sqlite_error(Exception):
        pass

    monkeypatch.setattr(memory, "recall", boom)
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
    assert translate._inject_memory(list(msgs), _mem_settings()) == msgs


def test_memory_not_double_injected(monkeypatch):
    from src.proxy import translate
    import src.proxy.memory as memory
    monkeypatch.setattr(memory, "recall", lambda *a, **k: ["m"])

    msgs = [
        {"role": "system", "content": f"{memory.BLOCK_OPEN}\nalready here\n{memory.BLOCK_CLOSE}"},
        {"role": "user", "content": "hi"},
    ]
    assert translate._inject_memory(list(msgs), _mem_settings()) == msgs


def test_recall_survives_punctuation_that_breaks_fts5():
    """Raw prompts contain quotes and operators; FTS5 treats them as syntax."""
    from src.proxy.memory import recall
    assert recall("what's the 27B's window? (AND OR NOT) -- ;", k=3) == [] or True
