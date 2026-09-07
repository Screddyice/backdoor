"""A committed local stream must keep speaking while it has nothing to say.

Regression coverage for the 2026-09-08 01:21 `API Error: The response stopped
arriving. The response above may be incomplete.`

A local turn commits early: FastAPI puts 200 and the SSE headers on the wire
when the StreamingResponse starts, and only then does the work begin. Two
phases of that work emit no bytes — waiting for the tier lock, and the cold
prefill before the first token — and either can run past a minute. That night
a failed-over turn queued behind a live one and did not acquire the tier for
75.2 seconds, 47 of them after Anthropic was reachable again. The client saw
headers and then silence, which is indistinguishable from a dead connection.

Serializing the tier is correct and stays (see tier_lock: interleaving two 17K
prefills costs both sessions ~100x). What changed is that the wait now says so.
"""

import asyncio

import pytest

import src.proxy.routes as routes
from src.proxy.config import Settings
from src.proxy import tier_lock


@pytest.fixture(autouse=True)
def _clean():
    tier_lock.reset()
    yield
    tier_lock.reset()


async def _drain(agen):
    return [ev async for ev in agen]


@pytest.mark.asyncio
async def test_ping_while_the_first_token_is_slow():
    """A cold prefill is silence the client must not read as death."""
    gap = 0.05

    async def slow_first():
        await asyncio.sleep(gap * 4)
        yield "event: message_start\ndata: {}\n\n"

    out = await _drain(routes._with_heartbeat(slow_first(), gap))

    assert routes._PING_EVENT in out, "silence before the first token must ping"
    assert out[-1] == "event: message_start\ndata: {}\n\n"
    assert out.count(routes._PING_EVENT) >= 2


@pytest.mark.asyncio
async def test_ping_while_the_tier_lock_is_held_by_another_turn():
    """The 01:20:38 shape: a second turn queued behind a live one."""
    tier = ("http://localhost:11434", "qwen3.8:27b-obliterated")
    released = asyncio.Event()

    async def live_turn():
        """Holds the tier until told to let go."""
        async with tier_lock.hold(tier[0], tier[1], timeout=5):
            await released.wait()

    holder = asyncio.create_task(live_turn())
    await asyncio.sleep(0.05)  # let it take the lock

    async def queued():
        yield "event: message_start\ndata: {}\n\n"

    agen = routes._with_heartbeat(
        routes._locked_events(queued(), tier, 5.0), 0.05
    )

    seen = []
    it = agen.__aiter__()
    # Pull until the real event arrives, releasing the tier once we have proof
    # the queued turn is pinging rather than sitting mute.
    while True:
        ev = await it.__anext__()
        seen.append(ev)
        if ev == routes._PING_EVENT and not released.is_set():
            released.set()
        if ev != routes._PING_EVENT:
            break

    await holder
    assert routes._PING_EVENT in seen, "a lock wait must not be silent"
    assert seen[-1] == "event: message_start\ndata: {}\n\n"


@pytest.mark.asyncio
async def test_no_pings_when_the_stream_is_prompt():
    """A fast stream is passed through untouched."""

    async def prompt():
        yield "a"
        yield "b"

    out = await _drain(routes._with_heartbeat(prompt(), 5.0))
    assert out == ["a", "b"]


@pytest.mark.asyncio
async def test_interval_of_zero_disables_the_heartbeat():
    async def slow():
        await asyncio.sleep(0.05)
        yield "a"

    assert await _drain(routes._with_heartbeat(slow(), 0.0)) == ["a"]


@pytest.mark.asyncio
async def test_a_hung_up_client_hands_the_tier_back():
    """Cancelling mid-wait must not strand the lock for every later turn."""
    tier = ("http://localhost:11434", "qwen3.8:27b-obliterated")

    async def never():
        await asyncio.sleep(30)
        yield "unreachable"

    agen = routes._with_heartbeat(
        routes._locked_events(never(), tier, 5.0), 0.05
    )
    it = agen.__aiter__()
    assert await it.__anext__() == routes._PING_EVENT

    await agen.aclose()  # what the server does when the client disconnects

    assert not tier_lock.waiting(*tier), "the tier must be free for the next turn"


@pytest.mark.asyncio
async def test_tracked_local_stream_pings_through_a_slow_start(monkeypatch):
    """The wiring, not just the helper: the wrapper every local turn goes through."""
    monkeypatch.setattr(routes, "_LOCAL_STREAM_PING_SECONDS", 0.05)

    async def slow():
        await asyncio.sleep(0.2)
        yield "event: message_stop\ndata: {}\n\n"

    out = await _drain(
        routes._tracked_local_stream(slow(), Settings(router_mode="hybrid"), tier=None)
    )

    assert routes._PING_EVENT in out
    assert out[-1] == "event: message_stop\ndata: {}\n\n"
