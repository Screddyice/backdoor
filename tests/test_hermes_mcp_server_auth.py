"""The boot guard, mirroring the one Hermes applies to its own API server, and
the per-request bearer check that enforces the same key on every call after
boot.

Hermes refuses to start its API server without a key of at least 16 characters,
even on loopback, and this bridge is exposed beyond loopback. Refusing at boot
is the only place the check is reliable: a bridge that starts with a weak key
and rejects requests later is a bridge someone will "fix" by removing the auth.

The HTTP-layer tests below drive the real Starlette ASGI app returned by
MCPServer.streamable_http_app() -- the same app main() serves under uvicorn --
via starlette.testclient.TestClient, so they exercise the actual auth
middleware stack (BearerAuthBackend, RequireAuthMiddleware) rather than a
mock. They run in stateless_http + json_response mode to avoid needing to
parse an SSE stream, which is not part of what is under test here.
"""

import pytest
from mcp.server.transport_security import TransportSecuritySettings
from starlette.testclient import TestClient

from src.hermes_mcp import http_server
from src.hermes_mcp.http_server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    MCP_PATH,
    MIN_KEY_LEN,
    KeyGuardError,
    PortGuardError,
    _allowed_hosts_from_env,
    _BridgeTokenVerifier,
    _host_from_env,
    _port_from_env,
    _transport_security,
    build_server,
    main,
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

    Accessor note: the brief's `server._tool_manager.list_tools()` reaches
    through a private attribute. `_tool_manager` is still present on the
    installed mcp SDK (mcp==2.0.0, which renamed FastMCP to MCPServer) -- it
    was not dropped -- but `MCPServer.list_tools()` is the public, async,
    supported accessor, so this test uses that instead and does not depend on
    a private attribute that carries no compatibility guarantee. The
    assertion itself is unchanged.
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


async def test_bridge_token_verifier_accepts_the_correct_token():
    """Unit-level coverage of _BridgeTokenVerifier.verify_token(), independent
    of the HTTP layer: the correct token returns an AccessToken carrying it."""
    verifier = _BridgeTokenVerifier(STRONG_KEY)
    result = await verifier.verify_token(STRONG_KEY)
    assert result is not None
    assert result.token == STRONG_KEY


async def test_bridge_token_verifier_rejects_the_wrong_token():
    verifier = _BridgeTokenVerifier(STRONG_KEY)
    result = await verifier.verify_token("not-the-configured-key-value-at-all")
    assert result is None


async def test_bridge_token_verifier_rejects_a_non_ascii_token_without_raising():
    """hmac.compare_digest raises TypeError on a non-ASCII str argument. A
    caller-controlled bearer token reaches verify_token as a str (Starlette
    decodes headers latin-1), so this must reject cleanly rather than raise --
    unit-level coverage that verify_token itself never raises, independent of
    whatever the HTTP layer around it does with the exception."""
    verifier = _BridgeTokenVerifier(STRONG_KEY)
    result = await verifier.verify_token("\xff\xfe not ascii")
    assert result is None


@pytest.mark.parametrize(
    "surrogate", ["\ud800", "\udc00"], ids=["lone-high-surrogate", "lone-low-surrogate"]
)
async def test_bridge_token_verifier_rejects_a_lone_surrogate_without_raising(surrogate):
    """U+D800 (high) and U+DC00 (low) are lone surrogates.
    str.encode("utf-8", errors="surrogateescape") is not total over them --
    surrogateescape only round-trips its own decode-side U+DC80-U+DCFF range,
    not arbitrary surrogates -- so without the try/except in verify_token this
    would raise UnicodeEncodeError instead of returning cleanly. Not reachable
    over HTTP (Starlette decodes header values with .decode("latin-1"), which
    is total over all 256 byte values and can never produce a surrogate), but
    verify_token must still be total over any str a caller could construct
    directly.

    Asserted against the wrong-token result, not a bare None check, so a
    regression that made the two paths return different-but-still-falsy
    values would also fail here.
    """
    verifier = _BridgeTokenVerifier(STRONG_KEY)
    wrong = await verifier.verify_token("not-the-configured-key-value-at-all")
    result = await verifier.verify_token(surrogate)
    assert result is None
    assert result == wrong


def test_allowed_hosts_from_env_is_empty_when_unset(monkeypatch):
    monkeypatch.delenv("HERMES_MCP_ALLOWED_HOSTS", raising=False)
    assert _allowed_hosts_from_env() == []


def test_allowed_hosts_from_env_strips_whitespace_and_drops_empty_entries(monkeypatch):
    monkeypatch.setenv(
        "HERMES_MCP_ALLOWED_HOSTS", " bridge.example.com:443 , , other.example.com:*  ,"
    )
    assert _allowed_hosts_from_env() == ["bridge.example.com:443", "other.example.com:*"]


def test_transport_security_is_none_when_unset(monkeypatch):
    """Unset HERMES_MCP_ALLOWED_HOSTS -> None, so main() passes nothing extra
    to MCPServer.run() and the SDK's own loopback-only default applies
    exactly as it did before this change."""
    monkeypatch.delenv("HERMES_MCP_ALLOWED_HOSTS", raising=False)
    assert _transport_security() is None


def test_transport_security_is_none_when_set_to_only_whitespace_and_commas(monkeypatch):
    monkeypatch.setenv("HERMES_MCP_ALLOWED_HOSTS", " , , ")
    assert _transport_security() is None


def test_transport_security_threads_configured_hosts_into_the_sdk_parameter(monkeypatch):
    """When set, the parsed hosts land in TransportSecuritySettings.allowed_hosts
    -- the same parameter mcp.server.lowlevel.server.Server.streamable_http_app
    passes to StreamableHTTPSessionManager as security_settings -- alongside
    the SDK's own loopback defaults, so local access is never revoked, only
    widened."""
    monkeypatch.setenv("HERMES_MCP_ALLOWED_HOSTS", "bridge.example.com:443")
    settings = _transport_security()
    assert isinstance(settings, TransportSecuritySettings)
    assert settings.enable_dns_rebinding_protection is True
    assert "bridge.example.com:443" in settings.allowed_hosts
    assert "127.0.0.1:*" in settings.allowed_hosts
    assert "localhost:*" in settings.allowed_hosts
    assert "[::1]:*" in settings.allowed_hosts


def test_main_threads_transport_security_into_run(monkeypatch):
    """main() must actually pass _transport_security()'s result to
    MCPServer.run(transport="streamable-http", ...) -- not just compute it.
    build_server() is stubbed out here so this exercises only that wiring,
    not a real server start."""
    monkeypatch.setenv("HERMES_MCP_KEY", STRONG_KEY)
    monkeypatch.setenv("HERMES_MCP_ALLOWED_HOSTS", "bridge.example.com:443")
    captured = {}

    class _FakeServer:
        def run(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(http_server, "build_server", lambda: _FakeServer())
    main()

    assert captured["transport"] == "streamable-http"
    settings = captured["transport_security"]
    assert isinstance(settings, TransportSecuritySettings)
    assert "bridge.example.com:443" in settings.allowed_hosts


def _clear_bind_env(monkeypatch):
    monkeypatch.delenv("HERMES_MCP_HOST", raising=False)
    monkeypatch.delenv("HERMES_MCP_PORT", raising=False)


def test_bind_defaults_are_unchanged_when_unset(monkeypatch):
    """Unset means bind exactly where the bridge binds today, which is the SDK's
    own default for streamable HTTP. Making the bind configurable must not move
    it for anyone who does not configure it."""
    _clear_bind_env(monkeypatch)
    assert (_host_from_env(), _port_from_env()) == (DEFAULT_HOST, DEFAULT_PORT)
    assert (DEFAULT_HOST, DEFAULT_PORT, MCP_PATH) == ("127.0.0.1", 8000, "/mcp")


@pytest.mark.parametrize("raw", ["", "   "])
def test_an_empty_bind_setting_is_treated_as_unset(monkeypatch, raw):
    monkeypatch.setenv("HERMES_MCP_HOST", raw)
    monkeypatch.setenv("HERMES_MCP_PORT", raw)
    assert (_host_from_env(), _port_from_env()) == (DEFAULT_HOST, DEFAULT_PORT)


def test_bind_settings_are_whitespace_tolerant(monkeypatch):
    """Same tolerance HERMES_MCP_ALLOWED_HOSTS already gives: a stray space in
    an EnvironmentFile line is a typo, not a different value."""
    monkeypatch.setenv("HERMES_MCP_HOST", "  0.0.0.0  ")
    monkeypatch.setenv("HERMES_MCP_PORT", "  12345  ")
    assert (_host_from_env(), _port_from_env()) == ("0.0.0.0", 12345)


@pytest.mark.parametrize("raw", ["eighty", "80.5", " 12 34 ", "0x1f90", "80,443"])
def test_a_non_integer_port_refuses_boot(monkeypatch, raw):
    """Rejected at boot, named in the message. Passing it through would surface
    much later as a bind failure with nothing pointing at the setting."""
    monkeypatch.setenv("HERMES_MCP_PORT", raw)
    with pytest.raises(PortGuardError) as e:
        _port_from_env()
    assert "HERMES_MCP_PORT" in str(e.value)


@pytest.mark.parametrize("raw", ["0", "-1", "65536", "99999"])
def test_an_out_of_range_port_refuses_boot(monkeypatch, raw):
    monkeypatch.setenv("HERMES_MCP_PORT", raw)
    with pytest.raises(PortGuardError) as e:
        _port_from_env()
    assert "HERMES_MCP_PORT" in str(e.value)


@pytest.mark.parametrize("raw", ["1", "65535"])
def test_the_range_boundaries_are_accepted(monkeypatch, raw):
    monkeypatch.setenv("HERMES_MCP_PORT", raw)
    assert _port_from_env() == int(raw)


def test_main_threads_the_configured_bind_into_run(monkeypatch):
    """Configuring the bind is only useful if main() actually passes it on.
    Without host= and port= the SDK defaults silently win, which is the state
    this replaces."""
    monkeypatch.setenv("HERMES_MCP_KEY", STRONG_KEY)
    monkeypatch.delenv("HERMES_MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("HERMES_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("HERMES_MCP_PORT", "12345")
    captured = {}

    class _FakeServer:
        def run(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(http_server, "build_server", lambda: _FakeServer())
    main()

    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 12345


def test_main_binds_the_defaults_when_the_bind_is_unset(monkeypatch):
    monkeypatch.setenv("HERMES_MCP_KEY", STRONG_KEY)
    monkeypatch.delenv("HERMES_MCP_ALLOWED_HOSTS", raising=False)
    _clear_bind_env(monkeypatch)
    captured = {}

    class _FakeServer:
        def run(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(http_server, "build_server", lambda: _FakeServer())
    main()

    assert (captured["host"], captured["port"]) == (DEFAULT_HOST, DEFAULT_PORT)


def test_main_refuses_to_start_on_a_bad_port(monkeypatch):
    """The guard has to fire before the server is built and run, or it is not a
    boot guard."""
    monkeypatch.setenv("HERMES_MCP_KEY", STRONG_KEY)
    monkeypatch.setenv("HERMES_MCP_PORT", "not-a-port")
    started = {"n": 0}

    class _FakeServer:
        def run(self, **kwargs):
            started["n"] += 1

    monkeypatch.setattr(http_server, "build_server", lambda: _FakeServer())
    with pytest.raises(PortGuardError):
        main()
    assert started["n"] == 0, "the server ran despite an unusable port"


def test_the_run_signature_accepts_the_bind_parameters():
    """host=, port= and transport_security= are forwarded by MCPServer.run()
    into run_streamable_http_async(). Pinned because they are **kwargs on run(),
    so a rename upstream would otherwise fail silently at runtime rather than
    here."""
    import inspect

    from mcp.server.mcpserver import MCPServer

    params = inspect.signature(MCPServer.run_streamable_http_async).parameters
    for name in ("host", "port", "transport_security"):
        assert name in params, f"MCPServer no longer accepts {name}"
    assert params["streamable_http_path"].default == MCP_PATH


#: Presented on the wrong-token path below. Not a member of PLACEHOLDERS and
#: not STRONG_KEY -- just some other string a caller might guess.
WRONG_TOKEN = "0123456789abcdef0123456789abcdef"

#: The MCP streamable-HTTP transport requires this Accept value on every POST.
ACCEPT_HEADERS = {"Accept": "application/json, text/event-stream"}

#: A minimal, spec-valid initialize request. Reused by every HTTP test below;
#: none of them depend on its contents beyond it being an initialize call.
INITIALIZE_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1"},
    },
}


@pytest.fixture
def http_client(monkeypatch):
    """A TestClient wrapping the real ASGI app build_server() produces --
    token_verifier and all -- with one extra, side-effect-free tool added so
    the "a tool call proceeds" test doesn't depend on a live gateway. The
    base_url uses a non-default port so httpx sends an explicit Host header
    (127.0.0.1:8000), matching the DNS-rebinding allow-list the SDK
    auto-configures for loopback hosts; a bare "127.0.0.1" or "localhost"
    Host header (no port) does not match that allow-list and is rejected
    with 421 before auth is even considered.
    """
    monkeypatch.setenv("HERMES_MCP_KEY", STRONG_KEY)
    server = build_server(REGISTRY)

    @server.tool()
    def _test_probe() -> str:
        return "ok"

    app = server.streamable_http_app(stateless_http=True, json_response=True)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        yield client


def test_missing_authorization_header_is_rejected(http_client):
    response = http_client.post("/mcp", json=INITIALIZE_BODY, headers=ACCEPT_HEADERS)
    assert response.status_code == 401


def test_wrong_token_is_rejected(http_client):
    headers = {**ACCEPT_HEADERS, "Authorization": f"Bearer {WRONG_TOKEN}"}
    response = http_client.post("/mcp", json=INITIALIZE_BODY, headers=headers)
    assert response.status_code == 401


def test_non_ascii_bearer_token_is_rejected_not_500(http_client):
    """hmac.compare_digest raises TypeError on a non-ASCII str, and Starlette's
    AuthenticationMiddleware only catches AuthenticationError -- so before the
    fix, this token reached compare_digest and the TypeError escaped as an
    unauthenticated 500 instead of a 401. Header keys/values are passed as
    bytes here (not str) so httpx's own default ascii header encoding doesn't
    reject the request client-side before it ever reaches the app; h11
    permits this obs-text on the wire, same as the finding describes.

    Asserted against the wrong-token response, not a bare status check, so a
    regression that makes the two paths distinguishable (a different body,
    a different status) also fails this test, not just a 500-specific one.
    """
    wrong_headers = {**ACCEPT_HEADERS, "Authorization": f"Bearer {WRONG_TOKEN}"}
    wrong = http_client.post("/mcp", json=INITIALIZE_BODY, headers=wrong_headers)

    non_ascii_headers = {
        b"Accept": ACCEPT_HEADERS["Accept"].encode(),
        b"Authorization": b"Bearer \xff",
    }
    non_ascii = http_client.post("/mcp", json=INITIALIZE_BODY, headers=non_ascii_headers)

    assert non_ascii.status_code == 401
    assert non_ascii.status_code == wrong.status_code
    assert non_ascii.text == wrong.text


@pytest.mark.parametrize(
    "headers",
    [ACCEPT_HEADERS, {**ACCEPT_HEADERS, "Authorization": f"Bearer {WRONG_TOKEN}"}],
    ids=["missing-header", "wrong-token"],
)
def test_rejection_responses_never_contain_the_key_or_the_presented_token(http_client, headers):
    response = http_client.post("/mcp", json=INITIALIZE_BODY, headers=headers)
    assert response.status_code == 401
    assert STRONG_KEY not in response.text
    assert WRONG_TOKEN not in response.text


def test_missing_header_and_wrong_token_responses_are_indistinguishable(http_client):
    """A future change that makes the missing-token and wrong-token rejection
    paths diverge (different status, different body, a header leak) should
    fail here -- the parametrized test above only asserts each response in
    isolation, so it would pass even if the two became distinguishable from
    each other."""
    missing = http_client.post("/mcp", json=INITIALIZE_BODY, headers=ACCEPT_HEADERS)
    wrong_headers = {**ACCEPT_HEADERS, "Authorization": f"Bearer {WRONG_TOKEN}"}
    wrong = http_client.post("/mcp", json=INITIALIZE_BODY, headers=wrong_headers)

    assert missing.status_code == wrong.status_code
    assert missing.text == wrong.text
    assert missing.headers == wrong.headers


def test_correct_token_is_accepted_and_a_tool_call_proceeds(http_client):
    auth_headers = {**ACCEPT_HEADERS, "Authorization": f"Bearer {STRONG_KEY}"}

    initialized = http_client.post("/mcp", json=INITIALIZE_BODY, headers=auth_headers)
    assert initialized.status_code == 200

    ack = http_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=auth_headers,
    )
    assert ack.status_code == 202

    called = http_client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "_test_probe", "arguments": {}},
        },
        headers=auth_headers,
    )
    assert called.status_code == 200
    body = called.json()
    assert body["result"]["content"][0]["text"] == "ok"
