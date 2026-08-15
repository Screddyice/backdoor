"""MCPServer app exposing Hermes gateways over streamable HTTP.

Two auth boundaries, deliberately distinct. Callers authenticate to the bridge
with HERMES_MCP_KEY. The bridge authenticates to each gateway with that
gateway's own key, named by the registry and read from the environment. A
caller never holds a gateway key.

The boot guard mirrors the one Hermes applies to its own API server. This
process binds loopback too, but a tunnel publishes it beyond the host, while
the gateway API servers it fronts are never exposed at all — so a weak key
here is the one weak key that actually reaches the network. A bridge that
boots with a weak key and rejects requests later is a bridge someone
eventually "fixes" by turning the auth off.

Note: the installed mcp SDK (mcp==2.0.0) renamed FastMCP to MCPServer and
dropped `mcp.server.fastmcp`; MCPServer is used here in its place. Its public
API (`.tool()`, `.list_tools()`, `.run(transport=...)`) is a drop-in match for
what this module needs from the old FastMCP name.
"""

from __future__ import annotations

import logging
import os

from mcp.server.mcpserver import MCPServer

from .registry import Profile, load_registry
from .tools import register_tools

logger = logging.getLogger(__name__)

MIN_KEY_LEN: int = 16
PLACEHOLDERS: frozenset[str] = frozenset(
    {"changeme", "change-me", "your-key-here", "replace_with_key",
     "replace-with-key", "secret", "password", "test", "example"}
)


class KeyGuardError(RuntimeError):
    """The bridge key is missing, too short, or a placeholder."""


def require_bridge_key() -> str:
    """Return HERMES_MCP_KEY, or refuse to boot. Never echoes the value."""
    value = os.environ.get("HERMES_MCP_KEY", "")
    if not value:
        raise KeyGuardError("HERMES_MCP_KEY is unset; refusing to start")
    if len(value) < MIN_KEY_LEN:
        raise KeyGuardError(
            f"HERMES_MCP_KEY is shorter than {MIN_KEY_LEN} characters; refusing to start"
        )
    lowered = value.strip().lower()
    if lowered in PLACEHOLDERS or len(set(lowered)) <= 2:
        raise KeyGuardError(
            "HERMES_MCP_KEY looks like a placeholder; refusing to start"
        )
    return value


def build_server(registry: dict[str, Profile] | None = None) -> MCPServer:
    """Build the MCPServer app. Applies the boot guard before registering anything."""
    require_bridge_key()
    if registry is None:
        registry = load_registry()
    logger.info(
        "hermes-mcp: %d profiles registered (%s)",
        len(registry),
        ", ".join(f"{n}:{p.tier}" for n, p in sorted(registry.items())),
    )
    mcp = MCPServer("hermes-mcp")
    register_tools(mcp, registry)
    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    build_server().run(transport="streamable-http")


if __name__ == "__main__":
    main()
