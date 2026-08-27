"""On-demand start and health gating for the MLX tier.

Every other local tier lives behind Ollama, which loads a model on first request
and evicts it on a timer. The Qwen3.8-27B Action-Abliterated tier does not: it is
a launchd-managed `mlx_vlm.server` on 127.0.0.1:8080 that is either running and
holding ~19GB, or not running at all. Nothing loads it lazily.

That matters because this tier now backs `/model qwen` AND offline failover.
Failover fires when the host is offline and nobody is watching, so "the server
happened to be stopped" would turn a working fallback into a dead session. So the
router starts the server itself and, when that fails, drops to the Ollama tier
rather than returning a connection error.

Cost of being wrong in each direction:
  * Starting when we should not: ~19GB resident that Ollama cannot evict, which
    collides with an llm-jury council. Bounded by `qwen38 stop`.
  * Not starting when we should: the offline session that failover exists to
    save gets nothing.

The second is worse, so this module starts the server.

subprocess note: launchctl is invoked through create_subprocess_exec with a
fixed argv and no shell. The only interpolated value is os.getuid(), an int.
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx

from . import ollama_admin

logger = logging.getLogger(__name__)

MLX_PROFILE = "local-qwen38-action"
MLX_HEALTH_URL = "http://127.0.0.1:8080/health"
MLX_LAUNCHD_LABEL = "com.aicollective.qwen38-mlx"

# Where a request goes when the MLX server will not come up. The 9B on Ollama
# loads lazily and needs no supervision, which is exactly what a fallback wants.
MLX_FALLBACK_PROFILE = "local-failover-heavy"

# Cold weight load measured at ~12s upstream for the 15GiB 4-bit artifact. The
# ceiling is generous because the alternative to waiting is failing, but it is
# finite because an HTTP client is blocked for the duration.
START_TIMEOUT_SECONDS = 90.0
PROBE_TIMEOUT_SECONDS = 2.0
POLL_INTERVAL_SECONDS = 1.5


async def is_healthy(timeout: float = PROBE_TIMEOUT_SECONDS) -> bool:
    """True when the MLX server answers /health."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(MLX_HEALTH_URL)
            return response.status_code == 200
    except Exception:
        return False


async def _kickstart() -> bool:
    """Ask launchd to start the daily-profile job. Idempotent."""
    service = f"gui/{os.getuid()}/{MLX_LAUNCHD_LABEL}"
    try:
        process = await asyncio.create_subprocess_exec(
            "launchctl",
            "kickstart",
            service,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
    except OSError as exc:
        logger.warning("mlx: launchctl kickstart failed to spawn: %s", exc)
        return False

    if process.returncode != 0:
        # The usual cause is a LaunchAgent that was never installed, which means
        # local/install-qwen38.sh has not run on this host.
        logger.warning(
            "mlx: launchctl kickstart %s exited %s: %s",
            service,
            process.returncode,
            stderr.decode(errors="replace").strip(),
        )
        return False
    return True


async def ensure_running(timeout: float = START_TIMEOUT_SECONDS) -> bool:
    """Return True when the MLX server is serving, starting it if needed."""
    if await is_healthy():
        return True

    logger.info("mlx: server is down, starting %s", MLX_LAUNCHD_LABEL)
    if not await _kickstart():
        return False

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        if await is_healthy():
            logger.info("mlx: server is ready")
            return True

    logger.warning("mlx: server did not become ready within %.0fs", timeout)
    return False


async def resolve_profile(profile: str) -> str:
    """Swap the MLX profile for the Ollama fallback when it cannot serve.

    Any other profile passes through untouched, so this is safe to call on every
    routed request.
    """
    if profile != MLX_PROFILE:
        return profile
    if await ensure_running():
        # The MLX tier is about to hold ~17 GB. An llm-jury council in Ollama
        # holds ~21 GB, and this host has 36 GB with a wired ceiling near 27, so
        # the two cannot co-reside -- getting it wrong has kernel-panicked this
        # machine twice. Nothing enforced it: the note above said "bounded by
        # `qwen38 stop`", which is a person remembering.
        #
        # Evicting is safe to do unconditionally here because this branch is
        # only reached when the MLX tier is confirmed running and about to
        # serve. It is a no-op when Ollama holds nothing, which is the normal
        # case, and never blocks the request: a failure to evict is logged and
        # the request proceeds, because refusing to answer would trade a memory
        # risk for a certain outage.
        try:
            await ollama_admin.evict_all(reason="MLX Qwen3.8 tier is serving")
        except Exception as error:  # noqa: BLE001 - housekeeping must not fail a request
            logger.warning("mlx: could not clear Ollama residency: %s", error)
        return profile
    logger.warning(
        "mlx: %s unavailable, falling back to %s. Run `qwen38 start` to restore it.",
        MLX_PROFILE,
        MLX_FALLBACK_PROFILE,
    )
    return MLX_FALLBACK_PROFILE
