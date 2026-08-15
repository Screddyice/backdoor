"""Tier enforcement. The safety-critical file.

A control_only profile is a hardened single-purpose endpoint: narrow SOUL.md,
low iteration cap, dedicated credentials, exposing an API server because its own
product calls it. It is not a general agent. Offering it as a chat target is the
exact mistake the tier exists to prevent, so the refusal is asserted here for
every chat-shaped tool by name, and the refusal has to say why — a generic error
invites a retry.

The list of chat-shaped tools is asserted too. Adding a conversational tool
without adding it to CHAT_TOOLS would silently open control_only profiles to it.
"""

import pytest

from src.hermes_mcp.registry import Profile
from src.hermes_mcp.tools import CHAT_TOOLS, check_tier, resolve

FULL = Profile(name="alpha", tier="full", port=9001, key_env="A", unit="a.service")
LOCKED = Profile(name="prod", tier="control_only", port=9002, key_env="B", unit="b.service")
GHOST = Profile(name="ghost", tier="unconfigured")


@pytest.mark.parametrize("tool", sorted(CHAT_TOOLS))
def test_control_only_refuses_every_chat_tool(tool):
    refusal = check_tier(LOCKED, tool)
    assert refusal is not None, f"{tool} was allowed against a control_only profile"
    assert refusal["state"] == "control_only"
    assert "control_only" in (refusal["reason"] or "")


@pytest.mark.parametrize("tool", sorted(CHAT_TOOLS))
def test_full_allows_every_chat_tool(tool):
    assert check_tier(FULL, tool) is None


@pytest.mark.parametrize("tool", ["hermes_status", "hermes_logs", "hermes_models"])
def test_control_only_allows_non_chat_tools(tool):
    assert check_tier(LOCKED, tool) is None, (
        "control_only means no conversation, not no observability"
    )


@pytest.mark.parametrize("tool", ["hermes_chat", "hermes_status", "hermes_logs"])
def test_unconfigured_refuses_everything(tool):
    refusal = check_tier(GHOST, tool)
    assert refusal is not None
    assert refusal["state"] == "unconfigured"
    assert refusal["reason"], "refusal must explain why, so caller does not retry"


def test_chat_tools_membership_is_pinned():
    """Pin the membership to detect removal or renaming of members. This test
    does NOT detect if a new conversational tool is added without being added
    to CHAT_TOOLS; verifying the tool surface requires the tool definitions,
    which arrive in a later task."""
    assert CHAT_TOOLS == frozenset(
        {"hermes_chat", "hermes_run_status", "hermes_run_stop", "hermes_run_approve"}
    )


def test_resolve_returns_the_profile_when_known():
    reg = {"alpha": FULL}
    profile, err = resolve(reg, "alpha")
    assert profile is FULL and err is None


def test_resolve_reports_an_unknown_profile_with_the_known_names():
    reg = {"alpha": FULL, "prod": LOCKED}
    profile, err = resolve(reg, "nope")
    assert profile is None
    assert "alpha" in err["reason"] and "prod" in err["reason"], (
        "an unknown profile should list what is available"
    )


@pytest.mark.parametrize("tool", ["hermes_chat", "hermes_logs"])
def test_unrecognised_tier_is_refused(tool):
    """A profile with a typo'd or unrecognised tier (e.g. 'control-only' with a
    hyphen) must be refused. Security gates fail closed."""
    bogus = Profile(name="typo", tier="control-only", port=9003, key_env="C", unit="c.service")
    refusal = check_tier(bogus, tool)
    assert refusal is not None
    assert refusal["state"] == "unconfigured"
    assert "control-only" in refusal["reason"], (
        "the reason must name the unrecognised tier"
    )
