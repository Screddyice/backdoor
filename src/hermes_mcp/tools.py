"""MCP tool definitions and the tier gate.

Tools are registered against a FastMCP instance by register_tools(), which
Tasks 4-6 fill in. This file starts with the gate because every tool consults
it first.
"""

from __future__ import annotations

import asyncio
from collections import deque
import logging
from pathlib import Path

from .client import GatewayClient, MissingKey, state
from .registry import Profile, TIERS

logger = logging.getLogger(__name__)

#: Tools that hold a conversation with an agent. Refused for control_only.
#: This set is pinned by tests/test_hermes_mcp_tiers.py to detect if an existing
#: member is removed or renamed. Paired with NON_CHAT_TOOLS below, whose union
#: against every tool actually registered on the MCP instance is what catches a
#: new conversational tool added without being added here.
CHAT_TOOLS = frozenset(
    {"hermes_chat", "hermes_run_status", "hermes_run_stop", "hermes_run_approve"}
)

#: Every registered tool that is not conversational. Together with CHAT_TOOLS,
#: this pins the full tool surface: tests/test_hermes_mcp_tiers.py asserts that
#: the set of tool names actually registered on the MCP instance equals
#: CHAT_TOOLS | NON_CHAT_TOOLS, so a tool added to neither set fails immediately.
NON_CHAT_TOOLS = frozenset(
    {
        "hermes_list",
        "hermes_status",
        "hermes_sessions",
        "hermes_session_messages",
        "hermes_models",
        "hermes_skills",
        "hermes_toolsets",
        "hermes_jobs",
        "hermes_start",
        "hermes_stop",
        "hermes_restart",
        "hermes_logs",
    }
)


def check_tier(profile: Profile, tool: str) -> dict | None:
    """Return None when *tool* may run against *profile*, else a refusal state."""
    if profile.tier == "unconfigured":
        return state(
            profile.name, "unconfigured",
            reason="this profile has no configuration and cannot be started",
            next="configure the profile, then add port and key_env to the registry",
        )
    if profile.tier == "control_only" and tool in CHAT_TOOLS:
        return state(
            profile.name, "control_only",
            reason=(
                f"{tool} holds a conversation, and this profile is registered "
                "control_only: it is a hardened single-purpose endpoint, not a "
                "general agent"
            ),
            next="use a profile registered as full, or change the tier deliberately",
        )
    if profile.tier == "delegate_only" and tool not in CHAT_TOOLS:
        return state(
            profile.name,
            "delegate_only",
            reason=f"{tool} is outside the delegate_only tool ceiling",
            next="use chat, run status, run approval, or run stop only",
        )
    if profile.tier not in TIERS:
        return state(
            profile.name, "unconfigured",
            reason=(
                f"unrecognised tier {profile.tier!r}; valid tiers are "
                f"{', '.join(repr(tier) for tier in sorted(TIERS))}"
            ),
            next="check the profile tier, or update the profile definition",
        )
    return None


def resolve(registry: dict[str, Profile], name: str) -> tuple[Profile | None, dict | None]:
    """Look up *name*, or return a refusal listing the profiles that do exist."""
    profile = registry.get(name)
    if profile is not None:
        return profile, None
    known = ", ".join(sorted(registry)) or "(none)"
    return None, state(
        name, "unconfigured",
        reason=f"no profile named {name!r} in the registry; known profiles: {known}",
        next="check the profile name, or add it to the registry",
    )


def register_tools(
    mcp,
    registry: dict[str, Profile],
    *,
    client_factory=GatewayClient,
    runner=None,
) -> None:
    """Register every bridge tool against *mcp*.

    client_factory is injectable so tests can supply a fake without patching
    module globals. runner is injectable the same way for the lifecycle tools,
    which shell out to `systemctl --user` instead of going through client_factory.
    """
    if runner is None:
        runner = asyncio.create_subprocess_exec

    async def _probe(profile: Profile) -> dict:
        """Probe one profile. Never raises: hermes_list fans out over these.

        Returns a bare state dict. hermes_list attaches the tier from the
        registry rather than from here, so every branch — including the two
        early returns — carries it.
        """
        try:
            if profile.tier == "unconfigured":
                return state(
                    profile.name, "unconfigured",
                    reason="no configuration; cannot be started",
                    next="configure the profile, then register port and key_env",
                )
            return await client_factory(profile).probe()
        except Exception as exc:
            logger.exception("probe failed for %s", profile.name)
            return state(
                profile.name, "unreachable",
                reason=f"probe raised {type(exc).__name__}",
                next="check the bridge logs",
            )

    async def _call(profile_name: str, tool: str, method: str, path: str,
                    json: dict | None = None) -> dict:
        profile, refusal = resolve(registry, profile_name)
        if refusal is not None:
            return refusal
        refusal = check_tier(profile, tool)
        if refusal is not None:
            return refusal
        try:
            return await client_factory(profile).request(method, path, json)
        except MissingKey as exc:
            return state(
                profile.name, "unconfigured",
                reason=str(exc),
                next="set it in the bridge environment file and restart the bridge",
            )

    @mcp.tool()
    async def hermes_list() -> dict:
        """List every registered Hermes profile with its state and tier."""
        names = list(registry)
        results = await asyncio.gather(*(_probe(registry[n]) for n in names))
        return {
            "profiles": [
                {**result, "tier": registry[name].tier}
                for name, result in zip(names, results)
            ]
        }

    @mcp.tool()
    async def hermes_status(profile: str) -> dict:
        """Health and capabilities for one profile."""
        return await _call(profile, "hermes_status", "GET", "/v1/capabilities")

    @mcp.tool()
    async def hermes_sessions(profile: str) -> dict:
        """List conversation sessions on a profile."""
        return await _call(profile, "hermes_sessions", "GET", "/api/sessions")

    @mcp.tool()
    async def hermes_session_messages(profile: str, session_id: str) -> dict:
        """Read the message history of one session."""
        return await _call(
            profile, "hermes_session_messages", "GET",
            f"/api/sessions/{session_id}/messages",
        )

    @mcp.tool()
    async def hermes_models(profile: str) -> dict:
        """Models available to a profile."""
        return await _call(profile, "hermes_models", "GET", "/v1/models")

    @mcp.tool()
    async def hermes_skills(profile: str) -> dict:
        """Skills installed on a profile."""
        return await _call(profile, "hermes_skills", "GET", "/v1/skills")

    @mcp.tool()
    async def hermes_toolsets(profile: str) -> dict:
        """Toolsets enabled on a profile."""
        return await _call(profile, "hermes_toolsets", "GET", "/v1/toolsets")

    @mcp.tool()
    async def hermes_jobs(profile: str) -> dict:
        """Scheduled jobs on a profile."""
        return await _call(profile, "hermes_jobs", "GET", "/api/jobs")

    @mcp.tool()
    async def hermes_chat(profile: str, message: str, session_id: str | None = None) -> dict:
        """Send a prompt to an agent and return its reply.

        Refused for control_only profiles. delegate_only profiles may use this
        run-scoped surface but cannot inspect sessions, configuration, logs, or
        lifecycle controls. Hermes applies its own approval layer; approvals
        raised inside the resulting run are answerable with hermes_run_approve,
        but approvals from chat-platform-initiated turns still surface on that
        platform, not here.
        """
        resolved, refusal = resolve(registry, profile)
        if refusal is not None:
            return refusal
        refusal = check_tier(resolved, "hermes_chat")
        if refusal is not None:
            return refusal
        if resolved.tier == "delegate_only" and session_id is not None:
            return state(
                resolved.name,
                "delegate_only",
                reason="delegate_only chat does not accept a caller-supplied session_id",
                next="start a new run without session_id",
            )
        body: dict = {"input": message}
        if session_id:
            body["session_id"] = session_id
        return await _call(profile, "hermes_chat", "POST", "/v1/runs", body)

    @mcp.tool()
    async def hermes_run_status(profile: str, run_id: str) -> dict:
        """Current status of a run started through this bridge."""
        return await _call(profile, "hermes_run_status", "GET", f"/v1/runs/{run_id}")

    @mcp.tool()
    async def hermes_run_stop(profile: str, run_id: str) -> dict:
        """Stop an in-flight run."""
        return await _call(profile, "hermes_run_stop", "POST", f"/v1/runs/{run_id}/stop")

    @mcp.tool()
    async def hermes_run_approve(profile: str, run_id: str, approved: bool) -> dict:
        """Answer an approval request raised inside a run."""
        return await _call(
            profile, "hermes_run_approve", "POST",
            f"/v1/runs/{run_id}/approval", {"approved": approved},
        )

    async def _systemctl(profile_name: str, verb: str) -> dict:
        profile, refusal = resolve(registry, profile_name)
        if refusal is not None:
            return refusal
        refusal = check_tier(profile, f"hermes_{verb}")
        if refusal is not None:
            return refusal
        if not profile.unit:
            return state(
                profile.name, "stopped",
                reason="no systemd unit is registered for this profile",
                next="run `hermes gateway install` for it, then add unit to the registry",
            )
        try:
            proc = await runner(
                "systemctl", "--user", verb, profile.unit,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
        except OSError as exc:
            return state(
                profile.name, "unreachable",
                reason=f"cannot run systemctl: {type(exc).__name__}",
                next="systemctl is not available on this host; the bridge must run "
                     "on the same host as the gateways, under systemd",
            )
        if proc.returncode != 0:
            return state(
                profile.name, "unreachable",
                reason=(err or out).decode("utf-8", "replace").strip()
                or f"systemctl {verb} exited {proc.returncode}",
                next="check `systemctl --user status` for the unit",
            )
        return state(profile.name, "ok", reason=f"systemctl {verb} succeeded")

    @mcp.tool()
    async def hermes_start(profile: str) -> dict:
        """Start a profile's gateway. Requires a registered systemd unit."""
        return await _systemctl(profile, "start")

    @mcp.tool()
    async def hermes_stop(profile: str) -> dict:
        """Stop a profile's gateway."""
        return await _systemctl(profile, "stop")

    @mcp.tool()
    async def hermes_restart(profile: str) -> dict:
        """Restart a profile's gateway.

        This resumes its messaging-platform connections, so side effects that
        were paused while it was down resume too.
        """
        return await _systemctl(profile, "restart")

    @mcp.tool()
    async def hermes_logs(profile: str, lines: int = 50) -> dict:
        """Tail a profile's gateway log."""
        p, refusal = resolve(registry, profile)
        if refusal is not None:
            return refusal
        refusal = check_tier(p, "hermes_logs")
        if refusal is not None:
            return refusal
        if not p.home:
            return state(
                p.name, "unconfigured",
                reason="no home is registered for this profile, so its log path is unknown",
                next="add home to the registry entry",
            )
        path = Path(p.home) / "logs" / "gateway.log"
        # A co-located gateway log can be hundreds of megabytes between
        # rotations. Reading the whole file to return a handful of lines would
        # block the event loop and spike memory, so bound both the read (a
        # deque over an open file handle keeps only the tail in memory) and
        # the request itself (a caller cannot ask for the whole file back).
        cap = max(0, min(lines, 1000))

        def _read_tail() -> list[str]:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                return [line.rstrip("\n") for line in deque(f, maxlen=cap)]

        try:
            # The deque bounds memory, but the read itself is synchronous and
            # walks the whole file, so on the large log described above it would
            # stall this single-process bridge for every profile, not just this
            # one. Off the event loop it stalls a worker thread instead, and the
            # per-profile isolation the tool surface promises holds. OSError
            # still propagates out of the thread, so the handling below is
            # unchanged.
            tail = await asyncio.to_thread(_read_tail)
        except OSError as exc:
            return state(
                p.name, "unreachable",
                reason=f"cannot read {path}: {type(exc).__name__}",
                next="check the profile home and whether the gateway has ever started",
            )
        return {**state(p.name, "ok"), "lines": tail}
