"""MCP tool definitions and the tier gate.

Tools are registered against a FastMCP instance by register_tools(), which
Tasks 4-6 fill in. This file starts with the gate because every tool consults
it first.
"""

from __future__ import annotations

from .client import state
from .registry import Profile

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
