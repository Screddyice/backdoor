"""The boot guard, mirroring the one Hermes applies to its own API server.

Hermes refuses to start its API server without a key of at least 16 characters,
even on loopback, and this bridge is exposed beyond loopback. Refusing at boot
is the only place the check is reliable: a bridge that starts with a weak key
and rejects requests later is a bridge someone will "fix" by removing the auth.
"""

import pytest

from src.hermes_mcp.http_server import (
    MIN_KEY_LEN,
    KeyGuardError,
    build_server,
    require_bridge_key,
)
from src.hermes_mcp.registry import Profile
from src.hermes_mcp.tools import CHAT_TOOLS, NON_CHAT_TOOLS

REGISTRY = {"alpha": Profile(name="alpha", tier="full", port=9001, key_env="A",
                             unit="a.service", home="/srv/a")}

#: A real generated key looks like this: plenty of distinct characters, not a
#: single character repeated. Forty repetitions of "k" is itself a placeholder
#: shape (character set of size 1), which is exactly what the entropy check in
#: require_bridge_key() exists to catch, so it must not be used as a fixture
#: for a key the guard is expected to accept.
STRONG_KEY = "9f2c1a7d4b8e6035af19c2d7e4b80516"


def test_a_strong_key_is_accepted(monkeypatch):
    monkeypatch.setenv("HERMES_MCP_KEY", STRONG_KEY)
    assert require_bridge_key() == STRONG_KEY


def test_a_missing_key_refuses_boot(monkeypatch):
    monkeypatch.delenv("HERMES_MCP_KEY", raising=False)
    with pytest.raises(KeyGuardError) as e:
        require_bridge_key()
    assert "HERMES_MCP_KEY" in str(e.value)


def test_a_short_key_refuses_boot(monkeypatch):
    monkeypatch.setenv("HERMES_MCP_KEY", "k" * (MIN_KEY_LEN - 1))
    with pytest.raises(KeyGuardError) as e:
        require_bridge_key()
    assert str(MIN_KEY_LEN) in str(e.value)


@pytest.mark.parametrize("value", [
    "changeme", "CHANGEME", "your-key-here", "replace_with_key", "xxxxxxxxxxxxxxxxx",
])
def test_placeholder_keys_refuse_boot(monkeypatch, value):
    monkeypatch.setenv("HERMES_MCP_KEY", value)
    with pytest.raises(KeyGuardError):
        require_bridge_key()


@pytest.mark.parametrize("case, key", [
    ("missing", None),
    ("short", "9f2c1a7d"),
    ("entropy", "a" * 32),
    ("placeholder", "replace_with_key"),
])
def test_the_guard_error_never_contains_the_key(monkeypatch, case, key):
    """Parametrized across all three raise sites in require_bridge_key(), so a
    debugging aid added to any single branch's message would still be caught
    here rather than slipping past an assertion that only ever exercised one
    branch.

    - missing: HERMES_MCP_KEY unset -> the "is unset" branch. There is no
      value to leak; the assertion is that the message still names the
      variable.
    - short: "9f2c1a7d" is 8 high-entropy characters, below MIN_KEY_LEN (16)
      -> the length branch, before the placeholder/entropy check ever runs.
    - entropy: "a" * 32 clears MIN_KEY_LEN but has a character set of size 1
      -> the placeholder branch, via its `len(set(lowered)) <= 2` arm.
    - placeholder: "replace_with_key" is exactly MIN_KEY_LEN (16) characters
      and a literal member of PLACEHOLDERS -> the same branch as "entropy",
      but via its `lowered in PLACEHOLDERS` arm instead.
    """
    if key is None:
        monkeypatch.delenv("HERMES_MCP_KEY", raising=False)
    else:
        monkeypatch.setenv("HERMES_MCP_KEY", key)
    with pytest.raises(KeyGuardError) as e:
        require_bridge_key()
    message = str(e.value)
    assert "HERMES_MCP_KEY" in message
    if key is not None:
        assert key not in message


async def test_build_server_registers_every_expected_tool(monkeypatch):
    """Assert against CHAT_TOOLS | NON_CHAT_TOOLS rather than a hardcoded list
    of tool names. Those two frozensets in tools.py are already the
    authoritative classification of every registered tool (Task 6 pins that
    their union equals the real registered surface), so this test tracks
    reality instead of duplicating a list that would silently drift as tools
    are added or renamed.

    Accessor note: the brief's `server._tool_manager.list_tools()` does not
    exist on the installed mcp SDK (mcp==2.0.0 renamed FastMCP to MCPServer
    and dropped the private _tool_manager attribute). The installed version
    exposes tools via the public, async `MCPServer.list_tools()` coroutine,
    so that is used here instead. The assertion itself is unchanged.
    """
    monkeypatch.setenv("HERMES_MCP_KEY", STRONG_KEY)
    server = build_server(REGISTRY)
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert names == CHAT_TOOLS | NON_CHAT_TOOLS


def test_build_server_refuses_without_a_key(monkeypatch):
    monkeypatch.delenv("HERMES_MCP_KEY", raising=False)
    with pytest.raises(KeyGuardError):
        build_server(REGISTRY)
