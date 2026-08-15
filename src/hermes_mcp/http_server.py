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

import hmac
import logging
import os

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from .registry import Profile, load_registry
from .tools import register_tools

logger = logging.getLogger(__name__)

MIN_KEY_LEN: int = 16
PLACEHOLDERS: frozenset[str] = frozenset(
    {"changeme", "change-me", "your-key-here", "replace_with_key",
     "replace-with-key", "secret", "password", "test", "example"}
)

# The SDK's own loopback-only default (mcp.server.lowlevel.server.Server.
# streamable_http_app, auto-applied when host is 127.0.0.1/localhost/::1 and
# no transport_security is given). Reused, not duplicated, when
# HERMES_MCP_ALLOWED_HOSTS adds extra hosts, so the loopback allowance never
# regresses -- only widens.
_DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = ("127.0.0.1:*", "localhost:*", "[::1]:*")
_DEFAULT_ALLOWED_ORIGINS: tuple[str, ...] = (
    "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
)

# The SDK's own defaults for MCPServer.run(transport="streamable-http"), which
# is what applied while main() passed neither. Restated here so the bind is
# explicit and documentable rather than implied, and kept identical so an
# unset environment binds exactly where it binds today. The endpoint path is
# the SDK default too and is not configurable here: a reverse proxy needs one
# unambiguous route, and /mcp is what the README and the unit document.
DEFAULT_HOST: str = "127.0.0.1"
DEFAULT_PORT: int = 8000
MCP_PATH: str = "/mcp"


class KeyGuardError(RuntimeError):
    """The bridge key is missing, too short, or a placeholder."""


class PortGuardError(RuntimeError):
    """HERMES_MCP_PORT is not an integer, or is outside 1-65535."""


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


class _BridgeTokenVerifier(TokenVerifier):
    """Checks a caller's bearer token against the bridge key.

    The key is read once, at construction, from the value require_bridge_key()
    already validated — never re-read from the environment per request.
    Comparison uses hmac.compare_digest, not ==, so a wrong guess cannot be
    timed apart from a right one character by character.

    hmac.compare_digest raises TypeError if either str argument contains a
    non-ASCII character. Header values arrive as str (Starlette decodes them
    latin-1, and h11 permits obs-text), so a non-ASCII bearer token would
    otherwise reach compare_digest and raise -- surfacing as an unauthenticated
    500 rather than a 401, since Starlette's AuthenticationMiddleware only
    catches AuthenticationError. Comparing encoded bytes instead sidesteps
    compare_digest's str-only restriction.

    str.encode(..., errors="surrogateescape") is itself not total: it raises
    UnicodeEncodeError on a lone surrogate (U+D800-U+DFFF), which surrogateescape's
    own decode-side round trip never produces but which a Python caller can still
    construct directly. That UnicodeEncodeError is caught below and treated as a
    non-matching token, so verify_token itself is total: no str input can raise,
    and a lone-surrogate token gets the same result as any other wrong token.
    """

    def __init__(self, key: str) -> None:
        # Only the encoded form is kept. verify_token compares bytes, so a
        # plaintext str copy would be retained for the process's whole life,
        # readable through vars() or a repr, and never read.
        self._key_bytes = key.encode("utf-8")

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            token_bytes = token.encode("utf-8", errors="surrogateescape")
        except UnicodeEncodeError:
            return None
        if hmac.compare_digest(token_bytes, self._key_bytes):
            return AccessToken(token=token, client_id="hermes-mcp-caller", scopes=[])
        return None


def build_server(registry: dict[str, Profile] | None = None) -> MCPServer:
    """Build the MCPServer app. Applies the boot guard before registering anything,
    then wires per-request bearer auth (via token_verifier) so every request after
    boot is checked too, not just the process's own startup.

    auth_server_provider and resource_server_url are both left unset, so the SDK
    mounts no OAuth discovery/authorization routes; issuer_url is a required
    AuthSettings field but is never read in that configuration, so it holds an
    inert placeholder rather than a real issuer.
    """
    key = require_bridge_key()
    if registry is None:
        registry = load_registry()
    logger.info(
        "hermes-mcp: %d profiles registered (%s)",
        len(registry),
        ", ".join(f"{n}:{p.tier}" for n, p in sorted(registry.items())),
    )
    mcp = MCPServer(
        "hermes-mcp",
        token_verifier=_BridgeTokenVerifier(key),
        auth=AuthSettings(issuer_url="http://localhost", resource_server_url=None),
    )
    register_tools(mcp, registry)
    return mcp


def _allowed_hosts_from_env() -> list[str]:
    """Parse HERMES_MCP_ALLOWED_HOSTS: comma-separated, whitespace-tolerant,
    empty entries ignored. Returns [] when unset or empty."""
    raw = os.environ.get("HERMES_MCP_ALLOWED_HOSTS", "")
    return [host.strip() for host in raw.split(",") if host.strip()]


def _transport_security() -> TransportSecuritySettings | None:
    """Build the DNS-rebinding Host allowlist for the streamable-HTTP transport,
    threaded into MCPServer.run() as its transport_security parameter (the
    same parameter mcp.server.lowlevel.server.Server.streamable_http_app
    passes to StreamableHTTPSessionManager as security_settings).

    HERMES_MCP_ALLOWED_HOSTS unset or empty -> None, so the SDK's own
    loopback-only default applies exactly as it does today (host defaults to
    "127.0.0.1", which auto-enables protection scoped to
    127.0.0.1/localhost/::1). Set -> the same loopback defaults plus the
    configured hosts, so a tunnel forwarding a public hostname in the Host
    header is accepted without disabling the protection.
    """
    extra = _allowed_hosts_from_env()
    if not extra:
        return None
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*_DEFAULT_ALLOWED_HOSTS, *extra],
        allowed_origins=[*_DEFAULT_ALLOWED_ORIGINS],
    )


def _host_from_env() -> str:
    """Parse HERMES_MCP_HOST: whitespace-tolerant, empty means unset. Returns
    DEFAULT_HOST when unset or empty, so the bind is unchanged by default.

    Note the interaction with HERMES_MCP_ALLOWED_HOSTS: the SDK auto-enables
    DNS-rebinding protection only when the host is loopback and no
    transport_security is supplied. Setting this to a non-loopback address
    without also setting HERMES_MCP_ALLOWED_HOSTS therefore leaves that
    protection off, which is why the README documents the pair together.
    """
    return os.environ.get("HERMES_MCP_HOST", "").strip() or DEFAULT_HOST


def _port_from_env() -> int:
    """Parse HERMES_MCP_PORT the same way. Returns DEFAULT_PORT when unset.

    Rejected at boot rather than passed through: an unusable value handed to
    the server surfaces much later, as a bind failure or a silently wrong port,
    and the operator has no line telling them which setting was at fault.
    """
    raw = os.environ.get("HERMES_MCP_PORT", "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        raise PortGuardError(
            f"HERMES_MCP_PORT is not an integer ({raw!r}); refusing to start"
        ) from None
    if not 1 <= port <= 65535:
        raise PortGuardError(
            f"HERMES_MCP_PORT is outside 1-65535 ({port}); refusing to start"
        )
    return port


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    host, port = _host_from_env(), _port_from_env()
    server = build_server()
    logger.info("hermes-mcp: serving MCP at http://%s:%d%s", host, port, MCP_PATH)
    server.run(
        transport="streamable-http",
        host=host,
        port=port,
        transport_security=_transport_security(),
    )


if __name__ == "__main__":
    main()
