"""Ollama residency control for local tiers the failover breaker claims.

Failing over is not free: the top tier (`qwen3.5:4b-256k`) loads at ~13 GB
allocated / ~9.7 GB in use on this 36 GB host, and on Apple Silicon that is
*wired* memory — it does not compress and it does not swap. While it is
resident the machine has that much less of everything.

Ollama's own release path is a single global `OLLAMA_KEEP_ALIVE` (5m here),
refreshed by every request. That is the wrong shape for a failover tier:

  * 5m of idle residency is priced for a model you are *using*. A failover tier
    is wanted only while the breaker is open, and the breaker closing is a
    precise, observable moment — there is no reason to keep guessing at it with
    a timer.
  * Because each request refreshes the timer, a burst of failed-over sessions
    pushes release further out the *more* sessions are affected. Measured
    2026-08-24: the breaker was open 22:09:31–22:24:41 (15 min) while 7 sessions
    retried; the tier was still resident ~9 min after the last local request.

So this module does two things, both best-effort:

  :func:`set_keep_alive`  clamp a tier's idle timer to something appropriate for
                          a tier nobody asked for (profiles set PROVIDER_KEEP_ALIVE).
  :func:`unload`          release it *now*, called when the breaker closes.

**Both use Ollama's NATIVE API, not the OpenAI-compatible one.** Verified
2026-08-24 against Ollama 0.32.13: `keep_alive` in a `/v1/chat/completions`
body is silently ignored and the model lands on the global 5m default, so the
clamp has to be a separate native call. A `/api/generate` with no `prompt`
neither generates nor prefills — it returns `done_reason: "load"` (or
`"unload"` for `keep_alive: 0`) and only touches the residency timer.

Every function here swallows its errors. A router that cannot talk to Ollama's
admin endpoint must still route; the failure mode of doing nothing is the old
behaviour (release on the global timer), which is degraded, not broken.
"""

import asyncio
import logging
import os
from urllib.parse import urlsplit

from httpx import AsyncClient

logger = logging.getLogger(__name__)

# Bound as a module attribute, not reached through `httpx.`, so a test can
# replace THIS name without patching the httpx module every other component
# (including the test's own ASGI client) is sharing.
__all__ = ["native_base", "is_local_base_url", "is_ollama", "set_keep_alive",
           "clamp_soon", "unload", "resident_models", "evict_all"]

# Reading residency (`/api/ps`) is a cheap lookup that answers immediately, so a
# short timeout is right: if the server cannot say what is loaded within five
# seconds, waiting longer will not produce a better answer.
ADMIN_READ_TIMEOUT = 5.0

# Mutating residency (`/api/generate` with a bare `keep_alive`) is NOT cheap,
# and five seconds was wrong for it. Ollama serialises work per model, so the
# call queues behind whatever that model is doing:
#
#   * a cold load — measured 2026-09-06, 8.2s for a 2.5 GB model, and the
#     default route tier is 17 GB;
#   * an in-flight inference — the clamp is issued around a request, so the
#     turn it belongs to is usually still generating.
#
# With the old 5s ceiling the call therefore timed out essentially always. The
# live router logged 11 clamp attempts between 2026-09-03 and 2026-09-06 and
# ZERO successes, so every route tier silently fell back to Ollama's global 5m
# idle timer — the exact cold-prefill cost the clamp exists to prevent, and
# invisible because httpx timeouts stringify to "" (see _admin_call).
#
# Waiting this long is only safe because no live request awaits it; the route
# path schedules the clamp through clamp_soon().
ADMIN_MUTATE_TIMEOUT = 120.0


def native_base(provider_base_url: str) -> str:
    """Ollama's native API root from a profile's OpenAI-compatible base URL.

    Profiles point at `http://localhost:11434/v1` because that is what the
    translation layer speaks. `keep_alive` lives one level up, on `/api`.
    """
    return provider_base_url.rstrip("/").removesuffix("/v1")


# A provider base URL is local when its HOSTNAME is loopback — not when the
# string contains a loopback-looking substring. "http://127.0.0.1.evil.com/v1"
# passed the substring test, which would grant keep-alive writes, unloads, and
# durable-memory injection to a remote host.
_LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def is_local_base_url(provider_base_url: str) -> bool:
    url = (
        provider_base_url
        if "://" in provider_base_url
        else f"http://{provider_base_url}"
    )
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return False
    return (host or "").lower() in _LOCAL_HOSTNAMES


def is_ollama(provider_base_url: str) -> bool:
    """Is this profile served by a local Ollama we may administer?

    Deliberately conservative. The admin calls are Ollama-specific, and a
    profile can point at a hosted provider (Modal, NVIDIA) where `/api/generate`
    means something else entirely or nothing at all. Only loopback qualifies:
    the whole justification for unloading is reclaiming *this* machine's memory,
    so a remote Ollama is not our residency to manage.
    """
    return is_local_base_url(provider_base_url)


async def _admin_call(provider_base_url: str, model: str, keep_alive) -> bool:
    if not model or not is_ollama(provider_base_url):
        return False
    url = f"{native_base(provider_base_url)}/api/generate"
    try:
        async with AsyncClient(timeout=ADMIN_MUTATE_TIMEOUT) as client:
            resp = await client.post(url, json={"model": model, "keep_alive": keep_alive})
        resp.raise_for_status()
        return True
    except Exception as exc:
        # Debug, not warning: the consequence of failing is that Ollama's global
        # timer releases the tier later than we wanted. Worth having in a log
        # when diagnosing residency, not worth a line in the normal path.
        #
        # The TYPE is logged as well as the message because httpx's timeout
        # exceptions carry an empty one: `str(httpx.ReadTimeout())` is "", so
        # the old format produced "... failed: " and named no cause at all.
        # That is how a clamp that never once succeeded stayed invisible.
        logger.debug("ollama admin %s keep_alive=%r failed: %s: %s",
                     model, keep_alive, type(exc).__name__, exc)
        return False


async def set_keep_alive(provider_base_url: str, model: str, duration: str) -> bool:
    """Clamp `model`'s idle residency to `duration` (e.g. "45s").

    Called after a failover request is dispatched, because Ollama resets the
    timer to the global default on every inference call — clamping once at load
    time would be undone by the second request.
    """
    if not duration:
        return False
    return await _admin_call(provider_base_url, model, duration)


# Clamps in flight, keyed by (base URL, model). Two jobs: hold a strong
# reference so the event loop cannot garbage-collect a running task, and keep
# one pending clamp per tier instead of one per request — they are idempotent,
# they serialise behind the same model anyway, and ADMIN_MUTATE_TIMEOUT is long
# enough that an unbounded pile would matter.
_clamps_in_flight: dict[tuple[str, str], asyncio.Task] = {}


def clamp_soon(provider_base_url: str, model: str, duration: str) -> None:
    """Clamp residency in the background, so no live request waits on it.

    The clamp has to survive a cold load or an in-flight inference, which is
    why ADMIN_MUTATE_TIMEOUT is minutes rather than seconds. Awaiting that on
    the request path would trade a late release for a stalled turn, which is
    the worse of the two, so the route path calls this instead of
    :func:`set_keep_alive` and never learns whether it worked.

    Ordering is not a problem: Ollama resets the idle timer on every inference,
    so a clamp that lands *after* the turn it was issued for is exactly the
    one that sticks.
    """
    if not duration or not model or not is_ollama(provider_base_url):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # no loop (sync context) — nothing to schedule onto
        return

    key = (provider_base_url, model)
    pending = _clamps_in_flight.get(key)
    if pending is not None and not pending.done():
        return

    task = loop.create_task(set_keep_alive(provider_base_url, model, duration))
    _clamps_in_flight[key] = task
    task.add_done_callback(lambda t: _clamps_in_flight.pop(key, None))


async def unload(provider_base_url: str, model: str) -> bool:
    """Evict `model` from the GPU now. `keep_alive: 0` is Ollama's unload verb."""
    ok = await _admin_call(provider_base_url, model, 0)
    if ok:
        logger.warning("released local tier %s — failover no longer needed", model)
    return ok


# Where a local Ollama listens when no profile named it. Profiles carry their
# own base URL, but the MLX tier is not an Ollama profile and still needs to
# reach the server to clear it.
DEFAULT_OLLAMA_BASE = os.environ.get("OLLAMA_HOST") or "http://localhost:11434"
if not DEFAULT_OLLAMA_BASE.startswith("http"):
    DEFAULT_OLLAMA_BASE = f"http://{DEFAULT_OLLAMA_BASE}"


async def resident_models(base_url: str = DEFAULT_OLLAMA_BASE) -> list[str]:
    """Models currently holding GPU memory, per Ollama's /api/ps.

    Empty on any failure. A router that cannot see the server must not conclude
    the GPU is busy and start evicting things it cannot name.
    """

    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    try:
        # AsyncClient via the module attribute, matching _admin_call: a test
        # replaces THIS name rather than patching httpx globally.
        async with AsyncClient(timeout=ADMIN_READ_TIMEOUT) as client:
            response = await client.get(f"{root}/api/ps")
            response.raise_for_status()
            payload = response.json()
    except Exception as error:  # noqa: BLE001 - any failure means "cannot tell"
        logger.debug("ollama: could not read residency: %s", error)
        return []
    models = payload.get("models")
    if not isinstance(models, list):
        return []
    return [m["name"] for m in models if isinstance(m, dict) and isinstance(m.get("name"), str)]


async def evict_all(base_url: str = DEFAULT_OLLAMA_BASE, *, reason: str = "") -> list[str]:
    """Unload every resident Ollama model. Returns the names actually evicted.

    THIS EXISTS TO STOP THE HOST PANICKING. The MLX Qwen3.8-27B tier holds
    roughly 17 GB and an llm-jury council holds roughly 21 GB; this machine has
    36 GB with a wired ceiling near 27 GB, so the two cannot co-reside. Getting
    that wrong has kernel-panicked this Mac twice, and nothing enforced it --
    mlx_admin only carried a comment saying it was "bounded by `qwen38 stop`",
    which is a person remembering, not a guard.

    Eviction is LOGGED at warning level, never silently: a jury run losing its
    council mid-flight should be explainable from the log rather than looking
    like the council crashed.
    """

    names = await resident_models(base_url)
    if not names:
        return []
    logger.warning(
        "ollama: evicting %d resident model(s) to free GPU memory%s: %s",
        len(names),
        f" ({reason})" if reason else "",
        ", ".join(names),
    )
    evicted: list[str] = []
    for name in names:
        if await unload(base_url, name):
            evicted.append(name)
        else:
            logger.warning("ollama: could not evict %s; memory pressure may persist", name)
    return evicted
