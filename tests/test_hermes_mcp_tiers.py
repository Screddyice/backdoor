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
from src.hermes_mcp.tools import CHAT_TOOLS, check_tier, register_tools, resolve

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


class _MCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *_a, **_k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class _Client:
    calls = []

    def __init__(self, profile, **_kw):
        self.profile = profile

    async def request(self, method, path, json=None):
        _Client.calls.append((self.profile.name, method, path, json))
        return {"ok": True, "data": {"run_id": "r-1"}}

    async def probe(self):
        return {"profile": self.profile.name, "state": "ok", "reason": None, "next": None}


def _tools():
    mcp = _MCP()
    register_tools(mcp, {"alpha": FULL, "prod": LOCKED, "ghost": GHOST},
                   client_factory=_Client)
    _Client.calls = []
    return mcp.tools


async def test_chat_reaches_a_full_profile():
    t = _tools()
    got = await t["hermes_chat"](profile="alpha", message="hello")
    assert got["ok"] is True
    name, method, path, body = _Client.calls[-1]
    assert (name, method) == ("alpha", "POST")
    assert body["input"] == "hello"
    assert "session_id" not in body, "session_id must be omitted when not provided"


async def test_chat_with_session_id_includes_it():
    """When session_id is provided, it must appear in the request body."""
    t = _tools()
    got = await t["hermes_chat"](profile="alpha", message="hello", session_id="s-1")
    assert got["ok"] is True
    name, method, path, body = _Client.calls[-1]
    assert body["session_id"] == "s-1"
    assert body["input"] == "hello"


async def test_chat_against_control_only_never_reaches_the_gateway():
    """The refusal must happen before any request. A gateway that answered
    would mean the tier is advisory, which is not what it is for."""
    t = _tools()
    got = await t["hermes_chat"](profile="prod", message="hello")
    assert got["state"] == "control_only"
    assert _Client.calls == [], "a request was sent to a control_only profile"


async def test_run_approve_posts_the_decision():
    t = _tools()
    await t["hermes_run_approve"](profile="alpha", run_id="r-9", approved=True)
    name, method, path, body = _Client.calls[-1]
    assert path == "/v1/runs/r-9/approval"
    assert body == {"approved": True}


async def test_run_stop_posts_to_the_run():
    t = _tools()
    await t["hermes_run_stop"](profile="alpha", run_id="r-9")
    assert _Client.calls[-1][2] == "/v1/runs/r-9/stop"


async def test_run_status_reads_the_run():
    t = _tools()
    await t["hermes_run_status"](profile="alpha", run_id="r-9")
    assert _Client.calls[-1][1:3] == ("GET", "/v1/runs/r-9")
