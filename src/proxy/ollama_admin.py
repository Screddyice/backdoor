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

import logging
import os

from httpx import AsyncClient

logger = logging.getLogger(__name__)

# Bound as a module attribute, not reached through `httpx.`, so a test can
# replace THIS name without patching the httpx module every other component
# (including the test's own ASGI client) is sharing.
__all__ = ["native_base", "is_ollama", "set_keep_alive", "unload", "resident_models", "evict_all"]

# Short: this is an out-of-band housekeeping call on localhost, and blocking a
# real request behind it would trade the problem for a worse one.
ADMIN_TIMEOUT = 5.0


def native_base(provider_base_url: str) -> str:
    """Ollama's native API root from a profile's OpenAI-compatible base URL.

    Profiles point at `http://localhost:11434/v1` because that is what the
    translation layer speaks. `keep_alive` lives one level up, on `/api`.
    """
    return provider_base_url.rstrip("/").removesuffix("/v1")


def is_ollama(provider_base_url: str) -> bool:
    """Is this profile served by a local Ollama we may administer?

    Deliberately conservative. The admin calls are Ollama-specific, and a
    profile can point at a hosted provider (Modal, NVIDIA) where `/api/generate`
    means something else entirely or nothing at all. Only loopback qualifies:
    the whole justification for unloading is reclaiming *this* machine's memory,
    so a remote Ollama is not our residency to manage.
    """
    url = provider_base_url.lower()
    return "localhost" in url or "127.0.0.1" in url or "0.0.0.0" in url


async def _admin_call(provider_base_url: str, model: str, keep_alive) -> bool:
    if not model or not is_ollama(provider_base_url):
        return False
    url = f"{native_base(provider_base_url)}/api/generate"
    try:
        async with AsyncClient(timeout=ADMIN_TIMEOUT) as client:
            resp = await client.post(url, json={"model": model, "keep_alive": keep_alive})
        resp.raise_for_status()
        return True
    except Exception as exc:
        # Debug, not warning: the consequence of failing is that Ollama's global
        # timer releases the tier later than we wanted. Worth having in a log
        # when diagnosing residency, not worth a line in the normal path.
        logger.debug("ollama admin %s keep_alive=%r failed: %s", model, keep_alive, exc)
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
        async with AsyncClient(timeout=ADMIN_TIMEOUT) as client:
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
