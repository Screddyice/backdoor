"""Local-tier residency: clamping keep-alive, and releasing on breaker close.

Regression cover for 2026-08-24. The breaker was open 22:09:31–22:24:41 while 7
sessions (242K–431K tokens post-strip) all failed over onto
`local-failover-256k`. That tier loads at ~13 GB allocated / ~9.7 GB in use, and
on Apple Silicon that is wired memory. It stayed resident ~9 minutes past the
close, because Ollama's only release mechanism is a global 5m idle timer that
every one of the outage's 138 requests refreshed.

Two independent guards, tested separately because either alone leaves a hole:

  * unload on close — precise, but only fires if the breaker actually closes.
  * PROVIDER_KEEP_ALIVE clamp — the backstop for an outage that ends with the
    sessions abandoned and no successful upstream call to close the breaker.
"""

import tempfile
from pathlib import Path

import pytest

from src.proxy import ollama_admin
from src.proxy.config import MODEL_ROUTES, load_profile_settings
from src.proxy.failover import FailoverBreaker

_STATE_DIR = Path(tempfile.mkdtemp(prefix="backdoor-residency-state-"))
_seq = iter(range(1_000_000))

OLLAMA = "http://localhost:11434/v1"


def make_breaker():
    return FailoverBreaker(
        threshold=2,
        now_fn=lambda: 1000.0,
        notify_fn=lambda *a: None,
        online_fn=lambda: False,
        state_path=_STATE_DIR / f"s-{next(_seq)}.json",
    )


# --------------------------------------------------------------------------
# URL handling
# --------------------------------------------------------------------------

def test_native_base_strips_openai_suffix():
    # Profiles point at the OpenAI-compatible endpoint; keep_alive lives on /api.
    assert ollama_admin.native_base("http://localhost:11434/v1") == "http://localhost:11434"
    assert ollama_admin.native_base("http://localhost:11434/v1/") == "http://localhost:11434"
    assert ollama_admin.native_base("http://127.0.0.1:11434") == "http://127.0.0.1:11434"


def test_only_loopback_is_administrable():
    assert ollama_admin.is_ollama("http://localhost:11434/v1")
    assert ollama_admin.is_ollama("http://127.0.0.1:11434/v1")
    # A hosted provider is not ours to unload — /api/generate there means
    # something else or nothing, and it is not this machine's memory.
    assert not ollama_admin.is_ollama("https://integrate.api.nvidia.com/v1")
    assert not ollama_admin.is_ollama("https://api.anthropic.com/v1")


# --------------------------------------------------------------------------
# Admin calls
# --------------------------------------------------------------------------

class FakeClient:
    """Stands in for httpx.AsyncClient, recording the JSON bodies it is sent."""

    calls: list[tuple[str, dict]] = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        FakeClient.calls.append((url, json))
        return self

    def raise_for_status(self):
        return None


@pytest.fixture
def fake_http(monkeypatch):
    FakeClient.calls = []
    monkeypatch.setattr(ollama_admin, "AsyncClient", FakeClient)
    return FakeClient


@pytest.mark.asyncio
async def test_set_keep_alive_uses_native_api(fake_http):
    # The OpenAI-compatible endpoint silently IGNORES keep_alive (verified
    # against Ollama 0.32.13), so the clamp must go to /api/generate or it is a
    # no-op that looks like it worked.
    assert await ollama_admin.set_keep_alive(OLLAMA, "qwen3.5:4b-256k", "45s")
    url, body = fake_http.calls[-1]
    assert url == "http://localhost:11434/api/generate"
    assert body == {"model": "qwen3.5:4b-256k", "keep_alive": "45s"}
    # No "prompt": this must not generate or prefill, only touch the timer.
    assert "prompt" not in body


@pytest.mark.asyncio
async def test_unload_sends_keep_alive_zero(fake_http):
    assert await ollama_admin.unload(OLLAMA, "qwen3.5:4b-256k")
    _, body = fake_http.calls[-1]
    assert body["keep_alive"] == 0


@pytest.mark.asyncio
async def test_admin_calls_skip_non_ollama_and_empty_model(fake_http):
    assert not await ollama_admin.unload("https://integrate.api.nvidia.com/v1", "llama")
    assert not await ollama_admin.set_keep_alive(OLLAMA, "", "45s")
    assert not await ollama_admin.set_keep_alive(OLLAMA, "m", "")  # unset = leave global
    assert fake_http.calls == []


@pytest.mark.asyncio
async def test_admin_failure_is_swallowed(monkeypatch):
    # A router that cannot reach Ollama's admin endpoint must still route. The
    # cost of failing is late release, which is the old behaviour, not an outage.
    class Boom(FakeClient):
        async def post(self, url, json=None):
            raise OSError("connection refused")

    monkeypatch.setattr(ollama_admin, "AsyncClient", Boom)
    assert await ollama_admin.unload(OLLAMA, "qwen3.5:4b-256k") is False


# --------------------------------------------------------------------------
# Breaker claim tracking
# --------------------------------------------------------------------------

def test_claims_are_recorded_and_drained_once():
    br = make_breaker()
    br.note_claim(OLLAMA, "qwen3.5:4b-256k")
    br.note_claim(OLLAMA, "qwen3.5:4b-256k")   # same tier, many sessions
    br.note_claim(OLLAMA, "qwen3.5:4b-128k")  # an outage can span two tiers

    assert br.drain_claims() == {
        (OLLAMA, "qwen3.5:4b-256k"),
        (OLLAMA, "qwen3.5:4b-128k"),
    }
    # Drained exactly once: a second close must not re-unload a tier that a
    # FRESH outage may already have re-claimed.
    assert br.drain_claims() == set()


def test_empty_model_is_not_claimed():
    br = make_breaker()
    br.note_claim(OLLAMA, "")
    assert br.drain_claims() == set()


def test_close_leaves_claims_for_the_caller():
    # record_success deliberately does NOT unload: failover.py stays pure
    # decision logic with no transport, so its state-machine tests need no HTTP.
    br = make_breaker()
    br.record_failure("ConnectTimeout")
    br.record_failure("ConnectTimeout")
    assert br.open
    br.note_claim(OLLAMA, "qwen3.5:4b-256k")

    br.record_success()
    assert not br.open
    assert br.drain_claims() == {(OLLAMA, "qwen3.5:4b-256k")}


# --------------------------------------------------------------------------
# Profile wiring
# --------------------------------------------------------------------------

def test_failover_only_tiers_clamp_keep_alive():
    for profile in ("local-failover-256k", "local-failover-128k"):
        assert load_profile_settings(profile).provider_keep_alive == "45s", profile


def test_route_reachable_tiers_do_not_clamp():
    # A deliberate `/model qwen` session that thinks for longer than the clamp
    # would evict its own 17 GB model and reload it next turn — slower, and more
    # memory churn than leaving it resident. Only unrequested tiers get clamped.
    for profile in set(MODEL_ROUTES.values()):
        assert load_profile_settings(profile).provider_keep_alive == "", profile


# --------------------------------------------------------------------------
# End-to-end wiring
#
# The 2026-08-24 incident was not a logic bug — the ladder picked correctly and
# the breaker closed on time. What was missing was the wiring between "breaker
# closed" and "tier released". So assert on what Ollama's admin endpoint
# ACTUALLY RECEIVES across a full outage-and-recovery cycle.
# --------------------------------------------------------------------------

import json

import httpx

import src.proxy.routes as routes
from src.proxy.app import create_app
from src.proxy.config import Settings, get_settings


class Recorder:
    async def complete(self, payload):
        return {
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": "local answer", "tool_calls": []},
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }


class _ByteStream(httpx.AsyncByteStream):
    """One-shot async body, so the relay's aiter_raw() has something to read."""

    def __init__(self, data: bytes):
        self._data = data

    async def __aiter__(self):
        yield self._data


def _flaky_upstream(fail_times: list[int]) -> httpx.AsyncClient:
    """Fails the first `fail_times[0]` SENDS, then answers: an outage that ends.

    Counts sends, not client requests. `_upstream_send` retries a transport
    failure once before giving up, so a caller that wants the breaker to see a
    failure has to fail both attempts.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        if fail_times[0] > 0:
            fail_times[0] -= 1
            raise httpx.ConnectTimeout("Network is unreachable")
        # A real async stream, not content=/json=: MockTransport sets eager
        # content, and the relay reads the upstream with aiter_raw(), which
        # rejects an already-consumed body (httpx.StreamConsumed).
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=_ByteStream(json.dumps({
                "id": "msg_x", "type": "message", "role": "assistant",
                "model": "claude-opus-5",
                "content": [{"type": "text", "text": "cloud"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }).encode()),
        )
    return httpx.AsyncClient(
        base_url="https://api.anthropic.com",
        transport=httpx.MockTransport(handler),
    )


async def _post(app, body: dict) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(
            "/v1/messages",
            content=json.dumps(body).encode(),
            headers={"content-type": "application/json"},
        )


def _huge_request() -> dict:
    # Post-strip size must clear the ladder's 28K bound so this lands on the
    # 256k tier — the expensive one the incident was about. A user message is
    # the right lever: make_bare truncates tool results, never the transcript.
    return {
        "model": "claude-opus-5",
        "messages": [{"role": "user", "content": "reconstruct this session. " * 30_000}],
    }


@pytest.mark.asyncio
async def test_outage_claims_and_clamps_then_recovery_unloads(monkeypatch, fake_http):
    # 2, not 1: `_upstream_send` absorbs a single transport failure by retrying
    # once, which is the point of that retry — one blip must not claim the GPU.
    # An outage fails both attempts, and only then does the breaker hear about it.
    routes._upstream_client = _flaky_upstream([2])
    # probe_interval=0 so the NEXT request re-probes upstream immediately. In
    # production this is 60s, which is also the real recovery latency: the
    # breaker cannot notice Anthropic is back until it is allowed to try, so
    # release lands within ~60s of recovery, not instantly.
    routes._breaker = FailoverBreaker(threshold=1, probe_interval=0, online_fn=lambda: False)
    monkeypatch.setattr(routes, "_get_profile_client", lambda profile, settings: Recorder())

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        router_mode="hybrid", failover_to_local=True, failover_threshold=1
    )
    try:
        # 1) Outage: served locally by the 256k tier, which must be clamped.
        assert (await _post(app, _huge_request())).status_code == 200

        clamps = [b for _, b in fake_http.calls if b["keep_alive"] == "45s"]
        assert clamps, f"tier was never clamped; admin calls: {fake_http.calls}"
        assert clamps[-1]["model"] == "qwen3.5:4b-256k"
        assert routes._breaker.open

        # 2) Recovery: upstream answers, breaker closes, tier must be released.
        assert (await _post(app, {"model": "claude-opus-5",
                                  "messages": [{"role": "user", "content": "hi"}]})).status_code == 200
        assert not routes._breaker.open

        unloads = [b for _, b in fake_http.calls if b["keep_alive"] == 0]
        assert unloads, f"tier never released on close; admin calls: {fake_http.calls}"
        assert unloads[-1]["model"] == "qwen3.5:4b-256k"

        # 3) Idempotent: a later success must not re-unload a tier a FRESH
        #    outage could by then have re-claimed.
        before = len(fake_http.calls)
        await _post(app, {"model": "claude-opus-5",
                          "messages": [{"role": "user", "content": "again"}]})
        assert len(fake_http.calls) == before
    finally:
        app.dependency_overrides.clear()
        routes._upstream_client = None
        routes._breaker = None


@pytest.mark.asyncio
async def test_deliberate_model_route_is_never_claimed(monkeypatch, fake_http):
    # `/model qwen` skips the breaker entirely. The user asked for that tier, so
    # the router must not clamp it or evict it when an unrelated outage ends.
    routes._upstream_client = _flaky_upstream([0])
    routes._breaker = FailoverBreaker(threshold=1, online_fn=lambda: False)
    monkeypatch.setattr(routes, "_get_profile_client", lambda profile, settings: Recorder())

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        router_mode="hybrid", failover_to_local=True, failover_threshold=1
    )
    try:
        resp = await _post(app, {"model": "qwen",
                                 "messages": [{"role": "user", "content": "hi"}]})
        assert resp.status_code == 200
        assert fake_http.calls == [], "a deliberate route must not be administered"
        assert routes._breaker.drain_claims() == set()
    finally:
        app.dependency_overrides.clear()
        routes._upstream_client = None
        routes._breaker = None


# --------------------------------------------------------------------------
# Releasing must wait for responses that are still generating
# --------------------------------------------------------------------------
# The breaker closes on the first upstream SUCCESS, and that success is a newer,
# different request than the failover streams still running. A local tier
# prefilling a large session emits nothing for minutes, so a stream dispatched
# during the outage is routinely still open when the outage ends — and the close
# was unloading the model out from under it.
#
# Observed 2026-08-26: a failover stream opened at 23:10:34 was still running
# when the breaker closed at 23:14:17 and released `qwen3.5:4b-256k` in the same
# 62ms window. That stream then produced nothing until it died on the
# 600-second read timeout at 23:20:38.

@pytest.fixture
def quiet_inflight():
    """Reset the module-level in-flight bookkeeping around each test."""
    from src.proxy import routes

    routes._local_inflight = 0
    routes._deferred_unloads = set()
    routes._deferred_evictions = set()
    try:
        yield routes
    finally:
        routes._local_inflight = 0
        routes._deferred_unloads = set()
        routes._deferred_evictions = set()
        routes._breaker = None


def _unloads(fake_http):
    return [b["model"] for _, b in fake_http.calls if b["keep_alive"] == 0]


@pytest.mark.asyncio
async def test_close_defers_release_while_a_failover_stream_is_open(
    quiet_inflight, fake_http
):
    routes = quiet_inflight
    br = make_breaker()
    br.record_failure("ConnectError")
    br.record_failure("ConnectError")
    assert br.open
    br.note_claim(OLLAMA, "qwen3.5:4b-256k")

    routes._local_stream_started()  # a response is mid-generation on that tier
    br.record_success()
    await routes._release_claims(br, Settings(router_mode="hybrid"))

    assert _unloads(fake_http) == [], "unloaded a tier a live stream was using"
    assert routes._deferred_unloads == {(OLLAMA, "qwen3.5:4b-256k")}


@pytest.mark.asyncio
async def test_last_stream_out_releases_the_deferred_tier(quiet_inflight, fake_http):
    routes = quiet_inflight
    br = make_breaker()
    br.record_failure("ConnectError")
    br.record_failure("ConnectError")
    br.note_claim(OLLAMA, "qwen3.5:4b-256k")
    settings = Settings(router_mode="hybrid")

    routes._local_stream_started()
    routes._local_stream_started()  # two sessions failed over onto one tier
    br.record_success()
    await routes._release_claims(br, settings)

    due = routes._local_stream_ended()
    assert due == set(), "released while the second stream was still generating"
    assert _unloads(fake_http) == []

    due = routes._local_stream_ended()
    assert due == {(OLLAMA, "qwen3.5:4b-256k")}
    routes._breaker = br  # _release_deferred re-checks the live breaker
    await routes._release_deferred(due, settings)
    assert _unloads(fake_http) == ["qwen3.5:4b-256k"]


@pytest.mark.asyncio
async def test_a_fresh_outage_keeps_the_tier_resident(quiet_inflight, fake_http):
    """Re-opening while we waited means the tier is claimed again.

    Unloading here would evict a model the NEW outage is already serving from,
    which is the same bug in the other direction.
    """
    routes = quiet_inflight
    br = make_breaker()
    br.record_failure("ConnectError")
    br.record_failure("ConnectError")
    br.note_claim(OLLAMA, "qwen3.5:4b-256k")
    settings = Settings(router_mode="hybrid")

    routes._local_stream_started()
    br.record_success()
    await routes._release_claims(br, settings)

    br.record_failure("ConnectError")  # the network drops again
    br.record_failure("ConnectError")
    assert br.open

    due = routes._local_stream_ended()
    routes._breaker = br
    await routes._release_deferred(due, settings)
    assert _unloads(fake_http) == []


@pytest.mark.asyncio
async def test_tracked_stream_clears_its_slot_on_error(quiet_inflight):
    """The count must come back down however the response ends.

    A stream that raises is the common case here — the 2026-08-26 one died on a
    read timeout — and a leaked slot would defer every later unload forever,
    which is worse than the race being fixed.
    """
    routes = quiet_inflight

    async def boom():
        yield "event: ping\n\n"
        raise httpx.ReadTimeout("upstream went away")

    tracked = routes._tracked_local_stream(boom(), Settings(router_mode="hybrid"))
    assert await tracked.__anext__() == "event: ping\n\n"
    assert routes._local_inflight == 1
    with pytest.raises(httpx.ReadTimeout):
        await tracked.__anext__()
    assert routes._local_inflight == 0


# --------------------------------------------------------------------------
# Escalation frees the tier it leaves
# --------------------------------------------------------------------------
# A `/model qwen` session that outgrows the 27B escalates to the 256K 4B, and
# until 2026-09-05 nothing freed the 27B: a route holds no breaker claim, so no
# close would ever release it, and Ollama keeps it for OLLAMA_KEEP_ALIVE.
#
# Measured 2026-09-05 with the 27B alone resident: 22.0 GB wired of a ~27 GB
# ceiling on a 36 GB host. The 256K 4B wants ~13 GB more, so the two do not fit
# and Ollama is free to try holding both (OLLAMA_MAX_LOADED_MODELS=3). The MLX
# side of the same collision has been guarded since 2026-08-24; this is the
# Ollama-to-Ollama half.

HEAVY = "qwen3.8:27b-obliterated"


@pytest.mark.asyncio
async def test_escalation_evicts_the_tier_it_left(quiet_inflight, fake_http):
    routes = quiet_inflight

    await routes._evict_outgoing_tier(OLLAMA, HEAVY)

    assert _unloads(fake_http) == [HEAVY], "the outgoing tier stayed resident"
    assert routes._deferred_evictions == set()


@pytest.mark.asyncio
async def test_escalation_waits_for_a_live_local_response(quiet_inflight, fake_http):
    """The 2026-08-26 stall, reached through the escalation door instead."""
    routes = quiet_inflight
    routes._local_stream_started()  # another session is generating on that tier

    await routes._evict_outgoing_tier(OLLAMA, HEAVY)

    assert _unloads(fake_http) == [], "evicted a tier a live response was using"
    assert routes._deferred_evictions == {(OLLAMA, HEAVY)}


@pytest.mark.asyncio
async def test_last_response_out_evicts_the_deferred_tier(quiet_inflight, fake_http):
    routes = quiet_inflight
    routes._local_stream_started()
    await routes._evict_outgoing_tier(OLLAMA, HEAVY)
    assert _unloads(fake_http) == []

    routes._local_stream_ended()
    await routes._drain_deferred_evictions()

    assert _unloads(fake_http) == [HEAVY]
    assert routes._deferred_evictions == set()


@pytest.mark.asyncio
async def test_a_deferred_eviction_holds_while_any_response_is_open(
    quiet_inflight, fake_http
):
    """Two concurrent responses: the first one out must not free the tier."""
    routes = quiet_inflight
    routes._local_stream_started()
    routes._local_stream_started()
    await routes._evict_outgoing_tier(OLLAMA, HEAVY)

    routes._local_stream_ended()
    await routes._drain_deferred_evictions()
    assert _unloads(fake_http) == [], "freed the tier with one response still open"

    routes._local_stream_ended()
    await routes._drain_deferred_evictions()
    assert _unloads(fake_http) == [HEAVY]


@pytest.mark.asyncio
async def test_a_route_response_defers_a_breaker_release_too(quiet_inflight, fake_http):
    """The in-flight count covers routes, not just failover.

    Before 2026-09-05 only failover streams were counted, so a breaker closing
    during a deliberate `/model qwen` response would unload the tier that
    response was generating on.
    """
    routes = quiet_inflight
    br = make_breaker()
    br.record_failure("ConnectError")
    br.record_failure("ConnectError")
    br.note_claim(OLLAMA, "qwen3.5:4b-256k")

    routes._local_stream_started()  # a ROUTE response, holding no claim
    br.record_success()
    await routes._release_claims(br, Settings(router_mode="hybrid"))

    assert _unloads(fake_http) == []
    assert routes._deferred_unloads == {(OLLAMA, "qwen3.5:4b-256k")}
