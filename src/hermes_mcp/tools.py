"""MCP tool definitions and the tier gate.

Tools are registered against a FastMCP instance by register_tools(), which
Tasks 4-6 fill in. This file starts with the gate because every tool consults
it first.
"""

from __future__ import annotations

import asyncio
import logging

from .client import GatewayClient, MissingKey, state
from .registry import Profile

logger = logging.getLogger(__name__)

#: Tools that hold a conversation with an agent. Refused for control_only.
#: This set is pinned by tests/test_hermes_mcp_tiers.py to detect if an existing
#: member is removed or renamed. It does NOT detect if a new conversational tool
#: is added without being added to this set; verifying the tool surface requires
#: the tool definitions themselves, which arrive in a later task.
CHAT_TOOLS = frozenset(
    {"hermes_chat", "hermes_run_status", "hermes_run_stop", "hermes_run_approve"}
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
    if profile.tier not in ("full", "control_only", "unconfigured"):
        return state(
            profile.name, "unconfigured",
            reason=f"unrecognised tier {profile.tier!r}; valid tiers are 'full', 'control_only', 'unconfigured'",
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


def register_tools(mcp, registry: dict[str, Profile], *, client_factory=GatewayClient) -> None:
    """Register every bridge tool against *mcp*.

    client_factory is injectable so tests can supply a fake without patching
    module globals.
    """

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
