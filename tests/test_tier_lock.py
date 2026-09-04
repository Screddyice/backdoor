"""One local inference at a time per tier.

Measured 2026-09-05, two ~45,600-token sessions alternating on
`qwen3.5:4b-256k`: A cold 57.5 s, B cold 114.3 s, then A again 105.8 s and B
again 116.3 s — both re-prefilled, because each request evicted the other's KV
cache. A single session repeating the same prompt costs 0.7 s and appending a
turn 1.6-2.4 s. Interleaving costs both sides roughly 100x; queueing costs the
second session one turn of waiting.
"""

import asyncio

import pytest

from src.proxy import tier_lock

OLLAMA = "http://localhost:11434/v1"


@pytest.fixture(autouse=True)
def _clean():
    tier_lock.reset()
    yield
    tier_lock.reset()


@pytest.mark.asyncio
async def test_one_tier_runs_one_response_at_a_time():
    live, overlapped = 0, False

    async def turn():
        nonlocal live, overlapped
        async with tier_lock.hold(OLLAMA, "qwen3.8:27b", timeout=5):
            live += 1
            overlapped = overlapped or live > 1
            await asyncio.sleep(0.05)
            live -= 1

    await asyncio.gather(*(turn() for _ in range(4)))

    assert not overlapped, "two responses generated on one tier at once"


@pytest.mark.asyncio
async def test_different_tiers_are_not_serialized_against_each_other():
    """Two tiers hold two caches. Queueing them would be pure lost throughput."""
    order = []

    async def turn(model, delay):
        async with tier_lock.hold(OLLAMA, model, timeout=5):
            await asyncio.sleep(delay)
            order.append(model)

    await asyncio.gather(turn("slow-tier", 0.08), turn("fast-tier", 0.01))

    assert order == ["fast-tier", "slow-tier"], (
        "a fast tier waited on a slow one; the lock must be per tier"
    )


@pytest.mark.asyncio
async def test_waiting_too_long_proceeds_instead_of_failing():
    """Degraded beats dropped: past the timeout the request runs unlocked."""
    held = asyncio.Event()

    async def hog():
        async with tier_lock.hold(OLLAMA, "busy", timeout=5):
            held.set()
            await asyncio.sleep(0.3)

    task = asyncio.create_task(hog())
    await held.wait()

    async with tier_lock.hold(OLLAMA, "busy", timeout=0.05) as locked:
        assert locked is False, "claimed the lock while another response held it"

    await task


@pytest.mark.asyncio
async def test_the_lock_is_released_when_a_response_raises():
    with pytest.raises(RuntimeError):
        async with tier_lock.hold(OLLAMA, "m", timeout=5):
            raise RuntimeError("stream died")

    assert not tier_lock.waiting(OLLAMA, "m"), "a failed response kept the tier locked"


@pytest.mark.asyncio
async def test_an_unnamed_tier_is_never_serialized():
    async with tier_lock.hold(OLLAMA, "", timeout=5) as locked:
        assert locked is False


@pytest.mark.asyncio
async def test_paired_acquire_and_release_serialize_too():
    """The Codex path reserves and releases in two different functions."""
    assert await tier_lock.acquire(OLLAMA, "m", timeout=5)
    assert tier_lock.waiting(OLLAMA, "m")

    # A second caller cannot take it while the first holds it.
    assert not await tier_lock.acquire(OLLAMA, "m", timeout=0.05)

    tier_lock.release(OLLAMA, "m")
    assert not tier_lock.waiting(OLLAMA, "m")


@pytest.mark.asyncio
async def test_releasing_twice_is_a_no_op():
    """The caller's release runs on several exit paths, one of them a
    BaseException handler. A double release on an asyncio.Lock raises
    RuntimeError, which would turn a slow response into a failed one."""
    assert await tier_lock.acquire(OLLAMA, "m", timeout=5)
    tier_lock.release(OLLAMA, "m")
    tier_lock.release(OLLAMA, "m")  # must not raise
    assert not tier_lock.waiting(OLLAMA, "m")


@pytest.mark.asyncio
async def test_releasing_what_was_never_taken_is_a_no_op():
    tier_lock.release(OLLAMA, "never-held")
