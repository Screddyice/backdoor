"""Lifecycle and logs bypass REST; they only work because the bridge is
co-located with the gateways.

Two refusal paths are asserted that a happy-path test would miss: a profile
with no systemd unit must refuse rather than shell out to `systemctl --user
start None`, and a profile with no home must refuse rather than build a log
path from nothing. The log path is always built from the registry-controlled
`p.home`, never from caller input, so path traversal via a crafted profile
name is not reachable in the first place and is not separately tested here.
"""

import pytest

from src.hermes_mcp.registry import Profile
from src.hermes_mcp.tools import register_tools

FULL = Profile(name="alpha", tier="full", port=9001, key_env="A",
               unit="a.service", home="/srv/gw/alpha")
NO_UNIT = Profile(name="nounit", tier="full", port=9003, key_env="C", home="/srv/gw/nounit")
NO_HOME = Profile(name="nohome", tier="full", port=9004, key_env="D", unit="d.service")
# unit is deliberately set here: if check_tier's unconfigured refusal were ever
# removed from _systemctl, this profile would fall through to the unit check,
# pass it, and reach the runner -- which is exactly the escape this guards.
UNCONFIGURED = Profile(name="ghost", tier="unconfigured", unit="e.service")


class _MCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *_a, **_k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class _Proc:
    def __init__(self, rc=0, out=b"", err=b""):
        self.returncode = rc
        self._out, self._err = out, err

    async def communicate(self):
        return self._out, self._err


def _tools(runner):
    mcp = _MCP()
    register_tools(
        mcp,
        {"alpha": FULL, "nounit": NO_UNIT, "nohome": NO_HOME, "ghost": UNCONFIGURED},
        client_factory=lambda p, **k: None,
        runner=runner,
    )
    return mcp.tools


@pytest.mark.parametrize("tool,verb", [
    ("hermes_start", "start"),
    ("hermes_stop", "stop"),
    ("hermes_restart", "restart"),
])
async def test_lifecycle_invokes_systemctl_user(tool, verb):
    seen = {}

    async def runner(*argv, **_kw):
        seen["argv"] = argv
        return _Proc()

    got = await _tools(runner)[tool](profile="alpha")
    assert seen["argv"] == ("systemctl", "--user", verb, "a.service")
    assert got["state"] == "ok"


async def test_lifecycle_refuses_a_profile_with_no_unit():
    called = {"n": 0}

    async def runner(*argv, **_kw):
        called["n"] += 1
        return _Proc()

    got = await _tools(runner)["hermes_start"](profile="nounit")
    assert got["state"] == "stopped"
    assert "unit" in (got["reason"] or "")
    assert called["n"] == 0, "shelled out for a profile with no systemd unit"


@pytest.mark.parametrize("tool", ["hermes_start", "hermes_stop", "hermes_restart"])
async def test_lifecycle_refuses_an_unconfigured_tier_profile(tool):
    """_systemctl runs resolve -> check_tier -> unit check -> runner. The
    no-unit path is covered above; this covers the tier path. UNCONFIGURED
    carries a unit precisely so that if check_tier's refusal were skipped, the
    call would fall through the unit check and reach the runner instead of
    failing for an unrelated reason.
    """
    called = {"n": 0}

    async def runner(*argv, **_kw):
        called["n"] += 1
        return _Proc()

    got = await _tools(runner)[tool](profile="ghost")
    assert got["state"] == "unconfigured"
    assert called["n"] == 0, "shelled out for a profile with an unconfigured tier"


async def test_lifecycle_reports_a_failing_systemctl():
    async def runner(*argv, **_kw):
        return _Proc(rc=1, err=b"Failed to start a.service")

    got = await _tools(runner)["hermes_start"](profile="alpha")
    assert got["state"] == "unreachable"
    assert "Failed to start" in (got["reason"] or "")


async def test_lifecycle_reports_a_missing_systemctl_binary():
    """systemctl not being on PATH (FileNotFoundError, e.g. any non-systemd
    host including a dev Mac) must come back as structured state, not an
    exception escaping the tool."""
    async def runner(*argv, **_kw):
        raise FileNotFoundError("systemctl not found")

    got = await _tools(runner)["hermes_start"](profile="alpha")
    assert got["state"] == "unreachable"
    assert "systemctl" in (got["reason"] or "").lower()


async def test_logs_read_from_the_profile_home(tmp_path):
    home = tmp_path / "alpha"
    (home / "logs").mkdir(parents=True)
    (home / "logs" / "gateway.log").write_text("line1\nline2\nline3\n", encoding="utf-8")

    profile = Profile(name="alpha", tier="full", port=9001, key_env="A",
                      unit="a.service", home=str(home))
    mcp = _MCP()
    register_tools(mcp, {"alpha": profile}, client_factory=lambda p, **k: None,
                   runner=None)
    got = await mcp.tools["hermes_logs"](profile="alpha", lines=2)
    assert got["lines"] == ["line2", "line3"]
    assert got["state"] == "ok"
    assert "reason" in got, "success shape must match every other tool's state() shape"


async def test_logs_cap_the_requested_lines_at_a_ceiling(tmp_path):
    """A caller cannot ask for the whole file back: lines is capped so the
    read stays bounded regardless of what the caller requests."""
    home = tmp_path / "alpha"
    (home / "logs").mkdir(parents=True)
    content = "".join(f"line{i}\n" for i in range(1005))
    (home / "logs" / "gateway.log").write_text(content, encoding="utf-8")

    profile = Profile(name="alpha", tier="full", port=9001, key_env="A",
                      unit="a.service", home=str(home))
    mcp = _MCP()
    register_tools(mcp, {"alpha": profile}, client_factory=lambda p, **k: None,
                   runner=None)
    got = await mcp.tools["hermes_logs"](profile="alpha", lines=5000)
    assert len(got["lines"]) == 1000
    assert got["lines"][0] == "line5"
    assert got["lines"][-1] == "line1004"


async def test_logs_refuse_a_profile_with_no_home():
    got = await _tools(None)["hermes_logs"](profile="nohome")
    assert got["state"] == "unconfigured"
    assert "home" in (got["reason"] or "")


async def test_logs_refuse_a_missing_log_file(tmp_path):
    profile = Profile(name="alpha", tier="full", port=9001, key_env="A",
                      unit="a.service", home=str(tmp_path))
    mcp = _MCP()
    register_tools(mcp, {"alpha": profile}, client_factory=lambda p, **k: None,
                   runner=None)
    got = await mcp.tools["hermes_logs"](profile="alpha")
    assert got["state"] == "unreachable"
