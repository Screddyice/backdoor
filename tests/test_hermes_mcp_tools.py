"""Fan-out must be isolated: one dead gateway cannot fail a listing.

hermes_list touches every profile, so the failure that matters is not a bad
response from one gateway but one gateway taking the whole call down with it.
That is asserted directly, with a registry containing a healthy profile, a
refusing profile, and an unconfigured one.
"""

import pytest

from src.hermes_mcp.client import MissingKey
from src.hermes_mcp.registry import Profile
from src.hermes_mcp.tools import register_tools

FULL = Profile(name="alpha", tier="full", port=9001, key_env="A", unit="a.service")
LOCKED = Profile(name="prod", tier="control_only", port=9002, key_env="B", unit="b.service")
GHOST = Profile(name="ghost", tier="unconfigured")
REGISTRY = {"alpha": FULL, "prod": LOCKED, "ghost": GHOST}


class FakeMCP:
    """Captures @mcp.tool() registrations so tests can call them directly."""

    def __init__(self):
        self.tools = {}

    def tool(self, *_a, **_k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class FakeClient:
    """A GatewayClient stand-in whose behaviour is chosen per profile."""

    behaviour: dict = {}

    def __init__(self, profile, **_kw):
        self.profile = profile

    async def request(self, method, path, json=None):
        return self.behaviour[self.profile.name]

    async def probe(self):
        return self.behaviour[self.profile.name]


@pytest.fixture
def tools():
    mcp = FakeMCP()
    register_tools(mcp, REGISTRY, client_factory=FakeClient)
    return mcp.tools


async def test_list_returns_every_profile_with_its_tier(tools):
    FakeClient.behaviour = {
        "alpha": {"profile": "alpha", "state": "ok", "reason": None, "next": None},
        "prod": {"profile": "prod", "state": "ok", "reason": None, "next": None},
        "ghost": {"profile": "ghost", "state": "unconfigured", "reason": "x", "next": "y"},
    }
    got = await tools["hermes_list"]()
    by_name = {p["profile"]: p for p in got["profiles"]}
    assert set(by_name) == {"alpha", "prod", "ghost"}
    assert by_name["prod"]["tier"] == "control_only"
    assert by_name["ghost"]["tier"] == "unconfigured"


async def test_one_dead_gateway_does_not_fail_the_listing(tools):
    FakeClient.behaviour = {
        "alpha": {"profile": "alpha", "state": "ok", "reason": None, "next": None},
        "prod": {"profile": "prod", "state": "stopped", "reason": "nothing listening",
                 "next": "start b.service"},
        "ghost": {"profile": "ghost", "state": "unconfigured", "reason": "x", "next": "y"},
    }
    got = await tools["hermes_list"]()
    by_name = {p["profile"]: p for p in got["profiles"]}
    assert by_name["alpha"]["state"] == "ok", "a stopped sibling degraded a healthy profile"
    assert by_name["prod"]["state"] == "stopped"
    assert by_name["prod"]["next"], "a stopped profile should say what to do"


async def test_a_raising_client_is_contained(tools):
    class Exploding(FakeClient):
        async def probe(self):
            if self.profile.name == "prod":
                raise RuntimeError("boom")
            return {"profile": self.profile.name, "state": "ok",
                    "reason": None, "next": None}

    mcp = FakeMCP()
    register_tools(mcp, REGISTRY, client_factory=Exploding)
    got = await mcp.tools["hermes_list"]()
    by_name = {p["profile"]: p for p in got["profiles"]}
    assert by_name["alpha"]["state"] == "ok"
    assert by_name["prod"]["state"] == "unreachable", (
        "an exception inside one probe must not escape hermes_list"
    )


async def test_unknown_profile_is_refused_by_name(tools):
    got = await tools["hermes_status"](profile="nope")
    assert got["state"] == "unconfigured"
    assert "alpha" in got["reason"]


@pytest.mark.parametrize("tool,path", [
    ("hermes_models", "/v1/models"),
    ("hermes_skills", "/v1/skills"),
    ("hermes_toolsets", "/v1/toolsets"),
    ("hermes_jobs", "/api/jobs"),
    ("hermes_sessions", "/api/sessions"),
])
async def test_config_tools_hit_the_documented_endpoint(tool, path):
    seen = {}

    class Recording(FakeClient):
        async def request(self, method, p, json=None):
            seen["path"] = p
            return {"ok": True, "data": {}}

    mcp = FakeMCP()
    register_tools(mcp, REGISTRY, client_factory=Recording)
    await mcp.tools[tool](profile="alpha")
    assert seen["path"] == path


async def test_session_messages_uses_the_session_id():
    seen = {}

    class Recording(FakeClient):
        async def request(self, method, p, json=None):
            seen["path"] = p
            return {"ok": True, "data": {}}

    mcp = FakeMCP()
    register_tools(mcp, REGISTRY, client_factory=Recording)
    await mcp.tools["hermes_session_messages"](profile="alpha", session_id="s-1")
    assert seen["path"] == "/api/sessions/s-1/messages"


async def test_config_tool_against_unconfigured_profile_refuses(tools):
    FakeClient.behaviour = {}
    got = await tools["hermes_models"](profile="ghost")
    assert got["state"] == "unconfigured"


async def test_missing_key_is_reported_as_unconfigured_not_raised():
    """client.request() raises MissingKey when the profile's key_env is unset
    in the bridge environment. _call must catch it and return structured
    state rather than let the traceback surface: an unset key is the single
    most likely misconfiguration on a fresh deployment, and nothing was
    rejected by a gateway here, so 'unconfigured' is the correct state, not
    'unauthorized'.
    """

    class NoKey(FakeClient):
        async def request(self, method, path, json=None):
            raise MissingKey(f"{self.profile.key_env} is unset")

    mcp = FakeMCP()
    register_tools(mcp, REGISTRY, client_factory=NoKey)
    got = await mcp.tools["hermes_status"](profile="alpha")
    assert got["state"] == "unconfigured"
    assert got["reason"]
