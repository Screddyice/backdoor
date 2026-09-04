"""One local inference at a time per tier, because the KV cache has one slot.

Measured 2026-09-05 against `qwen3.5:4b-256k`, two sessions of ~45,600 tokens
each, alternating:

    A cold                    57.5 s
    B cold                   114.3 s
    A again (both live)      105.8 s   <- re-prefilled from scratch
    B again (both live)      116.3 s   <- re-prefilled from scratch

A single session repeating the same prompt costs 0.7 s, and appending a turn to
it 1.6-2.4 s. The difference is not throughput contention; it is that each
session's request evicts the other's KV cache, so every turn becomes a cold
prefill. `OLLAMA_NUM_PARALLEL=2` provides two slots but not two retained
prefixes at these context sizes.

Interleaving therefore costs both sessions roughly 100x, while queueing costs
the second session one turn of waiting. The queue wins by a wide margin, so
local inference is serialized per (base_url, model).

Deliberately NOT a global lock: two DIFFERENT tiers hold different caches and
may run together, subject to the memory interlocks in mlx_admin and the
escalation eviction in routes. The lock is also never a reason to fail a
request — waiting past the timeout logs and proceeds, on the same principle as
the rest of this router's housekeeping: degraded is better than dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

logger = logging.getLogger(__name__)

_locks: dict[tuple[str, str], asyncio.Lock] = {}


def reset() -> None:
    """Drop every lock. Tests only."""
    _locks.clear()
    _held.clear()


def _lock_for(base_url: str, model: str) -> asyncio.Lock:
    key = (base_url or "", model or "")
    lock = _locks.get(key)
    if lock is None:
        lock = _locks[key] = asyncio.Lock()
    return lock


def waiting(base_url: str, model: str) -> bool:
    """True when a request is already generating on this tier."""
    key = (base_url or "", model or "")
    lock = _locks.get(key)
    return bool(lock and lock.locked())


@contextlib.asynccontextmanager
async def hold(base_url: str, model: str, *, timeout: float):
    """Serialize inference on one tier for the duration of the block.

    Held across the whole response, streaming included: releasing at first byte
    would let a second session start prefilling into the cache the first one is
    still generating from, which is the interleave this exists to prevent.
    """
    if not model or timeout <= 0:
        yield False
        return

    lock = _lock_for(base_url, model)
    if not lock.locked():
        await lock.acquire()
        try:
            yield True
        finally:
            lock.release()
        return

    started = time.monotonic()
    logger.info("tier %s busy — queueing behind the live response", model)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        # Proceeding unlocked reproduces the old interleaved behaviour, which is
        # slow. Refusing would reproduce nothing at all, which is worse.
        logger.warning(
            "tier %s still busy after %.0fs — proceeding without the lock; "
            "expect a cold prefill on both sessions",
            model, timeout,
        )
        yield False
        return
    waited = time.monotonic() - started
    if waited > 1.0:
        logger.info("tier %s acquired after %.1fs", model, waited)
    try:
        yield True
    finally:
        lock.release()


# Explicit acquire/release for callers whose response lifetime is not a single
# block. The Codex local path reserves and releases its slot in two different
# functions (see codex_routes._reserve_local_slot / _release_local_slot), so it
# cannot use `hold` — but it needs the same serialization, because a Codex turn
# and a Claude turn on the same tier evict each other exactly as two Claude
# turns do.
_held: set[tuple[str, str]] = set()


async def acquire(base_url: str, model: str, *, timeout: float) -> bool:
    """Take the tier. Returns False when it was not taken, and never raises.

    A False return is not a failure: it means proceed unserialized, which is
    the pre-2026-09-05 behaviour and merely slow.
    """
    if not model or timeout <= 0:
        return False
    lock = _lock_for(base_url, model)
    if lock.locked():
        logger.info("tier %s busy — queueing behind the live response", model)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning(
            "tier %s still busy after %.0fs — proceeding without the lock",
            model, timeout,
        )
        return False
    _held.add((base_url or "", model or ""))
    return True


def release(base_url: str, model: str) -> None:
    """Hand the tier back. Idempotent: releasing what was never taken is a no-op.

    Idempotence is the point. The caller's paired release runs on several exit
    paths including a BaseException handler, and a double release on an
    asyncio.Lock raises RuntimeError — which would turn a slow response into a
    failed one.
    """
    key = (base_url or "", model or "")
    if key not in _held:
        return
    _held.discard(key)
    lock = _locks.get(key)
    if lock is not None and lock.locked():
        lock.release()
