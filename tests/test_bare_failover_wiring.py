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
        router_mode="hybrid", failover_to_local=True, failover_threshold=1,
        qwen_memory=False,
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
            {"name": "mcp__example__crm_lookup",
             "description": "lookup", "input_schema": {}},
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
    assert "lost its network connection" in sent
    assert "When current information would improve the answer" not in sent


async def test_failed_over_request_keeps_mutation_tools_when_virtualization_is_disabled(
    offline_app,
):
    """The read-only outage policy belongs to the opt-in context feature."""
    app, recorder = offline_app

    await _post(app, _harness_request())

    names = [
        tool.get("function", {}).get("name", "")
        for tool in (recorder.payload.get("tools") or [])
    ]
    assert "Bash" in names


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
        failover_bare=False, qwen_memory=False,
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
        provider_model="qwen3.5:4b-64k",
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
        provider_model="qwen3.5:4b-64k",
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
        # This test isolates runtime supervision. Tier escalation has separate
        # coverage and would replace the runtime profile before this assertion.
        route_max_input_tokens=0,
        # The window guard is physical, not a policy, so opting out of the soft
        # limit above does not opt out of it — and the unstripped harness system
        # prompt alone is ~20K, past a 32K window at the measured 1.8 ratio.
        # Every real local profile strips it; so does this test now.
        route_bare=True,
    )
    try:
        resp = await _post(app, _route_request(turns=1))
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resolved == ["local-qwen38-obliterated"]
    assert seen[-1] == "local-fast"


async def test_oversized_route_session_is_trimmed_and_keeps_the_strong_tier(route_app):
    """A transcript that overflows the 27B's window is bounded, not exiled.

    This assertion is the reverse of the one it replaces, and the reversal was
    bought with measurements taken 2026-09-05 on this host. Escalating keeps
    every token and pays for it once, enormously: `qwen3.5:4b-256k` prefills
    103,277 tokens in 391 s, and the 2026-09-04 session that appeared frozen
    was about 142K. Trimming the same session to 18K and staying on the 27B
    costs roughly 70 s and leaves the stronger model answering.

    The ladder is still the fallback — see the next test — but it is no longer
    the first answer to a big transcript.
    """
    app, recorder, seen = route_app
    resp = await _post(app, _route_request(turns=20))

    assert resp.status_code == 200
    assert "local-failover-256k" not in seen, (
        "a trimmable session was sent to the wide 4B tier; bounding it keeps "
        "the 27B, which is both stronger and faster to prefill at this size"
    )
    sent = recorder.payload["messages"]
    assert len(sent) < 20, "the transcript was not bounded at all"


async def test_a_session_that_cannot_be_trimmed_still_escalates(route_app):
    """One message larger than the ceiling: nothing to drop, so the ladder runs.

    Bounding removes whole turns. When the newest turn alone is over the
    ceiling there is no smaller working set to build, and the wide tier is the
    only thing that can answer at all.
    """
    app, _recorder, seen = route_app
    huge = "Read this build log and explain the failure. " * 12_000
    resp = await _post(app, {
        "model": "qwen",
        "system": HARNESS_SYSTEM,
        "messages": [{"role": "user", "content": huge}],
    })

    assert resp.status_code == 200
    assert seen[-1] == "local-failover-256k", (
        f"un-trimmable session stayed on {seen[-1]}; the ladder is still the "
        "fallback when there is nothing to trim"
    )


# ---------------------------------------------------------------------------
# Ollama 0.32 rejects any system message that is not at index 0.
# ---------------------------------------------------------------------------

def test_stray_system_messages_fold_into_the_next_user_turn():
    """Ollama 0.32 500s on a system message at index > 0. Until 2026-09-05 the
    fix hoisted every stray one to the front, which satisfied Ollama and moved
    the prompt HEAD on every turn — Claude Code attaches a fresh system-reminder
    to most turns, so the cached prefix never matched again and a 7.7K-token
    turn cold prefilled in 29s instead of appending in 5-10s (`hoisting 3 system
    message(s) from position(s) [1, 4, 7]`, measured on a real routed session).
    The stray content is kept, folded into the turn it accompanies."""
    from src.proxy.translate import _hoist_system_messages

    out = _hoist_system_messages([
        {"role": "system", "content": "first"},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "stray"},
        {"role": "user", "content": "next"},
    ])

    assert [m["role"] for m in out] == ["system", "user", "user"], (
        f"a system message survived at index > 0: {[m['role'] for m in out]}"
    )
    assert out[0]["content"] == "first", "the head must not change between turns"
    assert out[1]["content"] == "hi", "an earlier turn was rewritten"
    assert "stray" in out[2]["content"] and out[2]["content"].endswith("next"), (
        "stray system content must be kept, attached to the turn it precedes"
    )


def test_folding_keeps_the_prefix_stable_as_reminders_accumulate():
    """The property the fold exists for: turn N+1's prompt must open with turn
    N's, even though the client sent one more system-reminder."""
    from src.proxy.translate import _hoist_system_messages

    turn_n = [
        {"role": "system", "content": "first"},
        {"role": "system", "content": "reminder 1"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]
    turn_n1 = turn_n + [
        {"role": "system", "content": "reminder 2"},
        {"role": "user", "content": "q2"},
    ]
    a = _hoist_system_messages(list(turn_n))
    b = _hoist_system_messages(list(turn_n1))

    assert b[: len(a)] == a, "the prefix moved; every turn would be a cold prefill"


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
    # Nothing follows it, so it becomes a trailing user note rather than a new
    # head — the head is the one place a late arrival must never land.
    assert [m["role"] for m in out] == ["user", "user"]
    assert out[0]["content"] == "hi"
    assert "late" in out[1]["content"]


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
    monkeypatch.setattr(memory, "recall", lambda *a, **k: ["the fast tier is qwen3.5:4b-64k"])

    out = translate._inject_memory(
        [{"role": "system", "content": "You are offline."}, {"role": "user", "content": "which tier?"}],
        _mem_settings(),
    )
    assert memory.BLOCK_OPEN in out[0]["content"]
    assert "qwen3.5:4b-64k" in out[0]["content"]
    assert "You are offline." in out[0]["content"], "the real system prompt must survive"


def test_memory_not_injected_for_cloud_provider(monkeypatch):
    """Cloud sessions get claude-mem through hooks, so proxy injection would duplicate it."""
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


@pytest.mark.asyncio
async def test_tier_escalation_frees_the_tier_it_left(monkeypatch):
    """Escalating must hand back the GPU, not stack a second tier beside it.

    The 27B holds 17 GB (22.0 GB wired, measured 2026-09-05 on a 36 GB host
    with a ~27 GB ceiling) and the 256K 4B wants ~13 GB more. Escalating
    without freeing the outgoing tier asks Ollama for both, and
    OLLAMA_MAX_LOADED_MODELS=3 lets it try.
    """
    recorder = RecordingClient()
    monkeypatch.setattr(routes, "_get_profile_client", lambda p, s: recorder)
    routes.set_provider_client(recorder)
    monkeypatch.setattr(routes, "_local_inflight", 0)

    evicted: list[str] = []

    async def _unload(base_url, model):
        evicted.append(model)
        return True

    monkeypatch.setattr(routes.ollama_admin, "unload", _unload)

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        router_mode="profile",
        provider_model="qwen3.5:4b-64k",
        route_max_input_tokens=28_000,
    )
    try:
        resp = await _post(app, _route_request(turns=20))
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert evicted == ["qwen3.5:4b-64k"], (
        f"escalation left {evicted or 'the outgoing tier'} resident; the tier it "
        "stopped using must be released before the wider one loads beside it"
    )


@pytest.mark.asyncio
async def test_a_session_that_stays_put_frees_nothing(monkeypatch):
    """No escalation, no eviction — the tier serving the request is not a leak."""
    recorder = RecordingClient()
    monkeypatch.setattr(routes, "_get_profile_client", lambda p, s: recorder)
    routes.set_provider_client(recorder)
    monkeypatch.setattr(routes, "_local_inflight", 0)

    evicted: list[str] = []

    async def _unload(base_url, model):
        evicted.append(model)
        return True

    monkeypatch.setattr(routes.ollama_admin, "unload", _unload)

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        router_mode="profile",
        provider_model="qwen3.5:4b-64k",
        route_max_input_tokens=28_000,
    )
    try:
        resp = await _post(app, _route_request(turns=1))
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert evicted == [], f"unloaded {evicted} for a session that never escalated"


@pytest.mark.asyncio
async def test_profile_mode_bounds_a_local_tier(monkeypatch):
    """The `qwen` wrapper runs profile mode, so this is the common local path.

    The backstop is gated on the provider being a local Ollama tier, which is
    what every `profiles/local-*.env` points at.
    """
    from src.proxy import working_set
    working_set.reset()
    recorder = RecordingClient()
    routes.set_provider_client(recorder)
    monkeypatch.setattr(routes, "_get_profile_client", lambda p, s: recorder)

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        router_mode="profile",
        provider_base_url="http://localhost:11434/v1",
        provider_model="qwen3.8:27b-obliterated",
        # Every profiles/local-*.env that reaches the 27B sets this. Without it
        # the untouched harness system prompt alone is ~20K of the ceiling, and
        # dropping turns cannot buy that back.
        route_bare=True,
    )
    try:
        resp = await _post(app, _route_request(turns=20))
    finally:
        app.dependency_overrides.clear()
        working_set.reset()

    assert resp.status_code == 200
    assert len(recorder.payload["messages"]) < 20, (
        "a profile-mode session reached the local tier untrimmed"
    )


@pytest.mark.asyncio
async def test_a_hosted_provider_is_never_bounded(monkeypatch):
    """The ceiling is this machine's GPU, not a property of the conversation."""
    from src.proxy import working_set
    working_set.reset()
    recorder = RecordingClient()
    routes.set_provider_client(recorder)
    monkeypatch.setattr(routes, "_get_profile_client", lambda p, s: recorder)

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        router_mode="profile",
        provider_base_url="https://integrate.api.nvidia.com/v1",
        provider_model="some-hosted-model",
    )
    try:
        resp = await _post(app, _route_request(turns=20))
    finally:
        app.dependency_overrides.clear()
        working_set.reset()

    assert resp.status_code == 200
    # 21, not 20: the translation layer hoists the system prompt into the
    # message list. The point is that no TURN was dropped.
    assert len(recorder.payload["messages"]) == 21, (
        "trimmed a hosted provider's request for a local window it does not have"
    )


# ---------------------------------------------------------------------------
# The provider's window, in the provider's tokens
# ---------------------------------------------------------------------------
# Measured 2026-09-05 on a real routed session: a request the router estimated
# at 16,706 tokens arrived at Ollama as 29,640 (ratio 1.77). The 22K working set
# at that ratio is 39K, past the 27B's 32K window; Ollama truncated from the
# front, the model lost its instructions, and two turns came back empty.


def test_the_window_guard_leaves_room_for_a_request_s_fixed_overhead():
    """A guard below the irreducible cost of a request is an off switch.

    Shipped at ratio 1.8 for one revision, the guard came to 15,095 — under the
    ~19K the system block and tool schemas cost by the same estimate — so the
    working set could never fit and every session fell through to the ladder.
    Observed on a real session: "cannot reach 15095 tokens (tail alone is
    19877)", five turns in a row.
    """
    from src.proxy.config import Settings
    from src.proxy.routes import _window_guard

    local = Settings(provider_base_url="http://localhost:11434/v1",
                     provider_context_tokens=32_768, provider_max_tokens=4_096)
    guard = _window_guard(local)
    assert guard >= 20_000, (
        f"guard {guard} is at or under the fixed overhead of a tool-carrying "
        "request; the working set could never fit inside it"
    )
    assert guard < 32_768, "the guard must still be under the provider window"


def test_a_narrow_tier_still_binds_the_ceiling():
    """The guard exists for a tier whose window is genuinely smaller."""
    from src.proxy.config import Settings
    from src.proxy.routes import _window_guard

    narrow = Settings(provider_base_url="http://localhost:11434/v1",
                      provider_context_tokens=16_384, provider_max_tokens=4_096)
    assert _window_guard(narrow) < Settings().local_working_set_max_tokens


def test_a_wide_tier_keeps_the_configured_ceiling():
    from src.proxy.config import Settings
    from src.proxy.routes import _window_guard

    wide = Settings(provider_base_url="http://localhost:11434/v1",
                    provider_context_tokens=262_144, provider_max_tokens=8_192)
    assert _window_guard(wide) > Settings().local_working_set_max_tokens


def test_a_hosted_provider_has_no_window_guard():
    from src.proxy.config import Settings
    from src.proxy.routes import _window_guard

    assert _window_guard(Settings(provider_base_url="https://integrate.api.nvidia.com/v1")) == 0


def test_reaching_the_window_is_logged_loudly(caplog):
    import logging
    from src.proxy.config import Settings
    from src.proxy.routes import _note_provider_count

    s = Settings(provider_base_url="http://localhost:11434/v1",
                 provider_context_tokens=32_768, provider_max_tokens=4_096)
    with caplog.at_level(logging.WARNING, logger="src.proxy.routes"):
        _note_provider_count("qwen3.8:27b-obliterated", 20_000, 29_640, s)
    assert any("reached the window" in r.message for r in caplog.records), (
        "an overflow must name itself; Ollama truncates from the front silently"
    )


def test_an_ordinary_prompt_records_the_ratio_without_warning(caplog):
    """The pairs this logs are how the ratio gets set from evidence rather than
    belief — the previous default was wrong precisely because it was not."""
    import logging
    from src.proxy.config import Settings
    from src.proxy.routes import _note_provider_count

    s = Settings(provider_base_url="http://localhost:11434/v1",
                 provider_context_tokens=32_768, provider_max_tokens=4_096)
    with caplog.at_level(logging.INFO, logger="src.proxy.routes"):
        _note_provider_count("qwen3.8:27b-obliterated", 66_016, 26_832, s)
    assert any("ratio 0.41" in r.message for r in caplog.records), [r.message for r in caplog.records]
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)
