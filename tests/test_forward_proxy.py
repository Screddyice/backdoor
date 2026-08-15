"""Tests for the CONNECT forward proxy.

The proxy exists so `ANTHROPIC_BASE_URL` can stay unset — that variable is what
Claude Code checks before offering Remote Control — while the router still sees
every request. It does that by sitting in `HTTPS_PROXY` instead.

Two behaviours carry the whole design and are tested here:

1. **Allowlisted hosts are intercepted.** `api.anthropic.com` is TLS-terminated
   with a locally minted leaf and the plaintext is spliced into the router, so
   `/model qwen` routing and offline failover keep working.

2. **Everything else is tunnelled blind.** A single `claude` session also talks
   to Composio, mem0, Neon, PostHog, npm and — critically — the Remote Control
   bridge on claude.ai. Intercepting any of those would break certificate
   pinning at best and silently reroute traffic at worst. They must pass through
   as opaque bytes.

Client work runs in threads via `asyncio.to_thread`: blocking sockets express
"CONNECT, then upgrade this same socket to TLS" far more directly than
`loop.start_tls`, and the clarity matters more than the thread here.
"""

import asyncio
import socket
import ssl

import pytest

from src.proxy.ca import LocalCA
from src.proxy.forward import ForwardProxy

MITM_HOST = "api.anthropic.com"

ROUTER_REPLY = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Type: application/json\r\n"
    b"Content-Length: 16\r\n"
    b"Connection: close\r\n"
    b"\r\n"
    b'{"ok":"router"}\n'
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _read_headers(sock: socket.socket) -> bytes:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


def _read_all(sock: socket.socket) -> bytes:
    buf = b""
    while True:
        try:
            chunk = sock.recv(4096)
        except (ssl.SSLError, OSError):
            break
        if not chunk:
            break
        buf += chunk
    return buf


class StubRouter:
    """Stands in for uvicorn on :8083 — records one request, replies, closes."""

    def __init__(self) -> None:
        self.requests: list[bytes] = []
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    async def _handle(self, reader, writer) -> None:
        data = await reader.readuntil(b"\r\n\r\n")
        self.requests.append(data)
        writer.write(ROUTER_REPLY)
        await writer.drain()
        writer.close()

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


class EchoServer:
    """A plain TCP peer, used to prove non-allowlisted traffic is untouched."""

    def __init__(self) -> None:
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    async def _handle(self, reader, writer) -> None:
        data = await reader.read(64)
        writer.write(b"PONG:" + data)
        await writer.drain()
        writer.close()

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


@pytest.fixture
async def router():
    stub = StubRouter()
    await stub.start()
    yield stub
    await stub.stop()


@pytest.fixture
async def proxy(tmp_path, router):
    p = ForwardProxy(
        listen_host="127.0.0.1",
        listen_port=0,
        mitm_hosts={MITM_HOST},
        router_host="127.0.0.1",
        router_port=router.port,
        ca=LocalCA(tmp_path / "ca"),
    )
    await p.start()
    yield p
    await p.stop()


# ── tests ────────────────────────────────────────────────────────────────────


async def test_intercepts_allowlisted_host_and_reaches_the_router(proxy, router):
    def client() -> bytes:
        sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
        sock.sendall(
            f"CONNECT {MITM_HOST}:443 HTTP/1.1\r\nHost: {MITM_HOST}:443\r\n\r\n".encode()
        )
        assert b"200" in _read_headers(sock)

        ctx = ssl.create_default_context(cafile=str(proxy.ca.ca_cert_path))
        tls = ctx.wrap_socket(sock, server_hostname=MITM_HOST)
        tls.sendall(
            b"POST /v1/messages HTTP/1.1\r\n"
            b"Host: api.anthropic.com\r\n"
            b"Connection: close\r\n"
            b"Content-Length: 0\r\n"
            b"\r\n"
        )
        return _read_all(tls)

    body = await asyncio.to_thread(client)

    assert b'{"ok":"router"}' in body
    assert len(router.requests) == 1
    assert router.requests[0].startswith(b"POST /v1/messages")


async def test_intercepted_leaf_is_rejected_without_the_ca(proxy):
    """A client that does not trust our CA must fail the handshake.

    This is the guard that keeps the CA from being quietly load-bearing for
    anything beyond the one process we hand it to.
    """

    def client() -> None:
        sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
        sock.sendall(f"CONNECT {MITM_HOST}:443 HTTP/1.1\r\n\r\n".encode())
        _read_headers(sock)

        ctx = ssl.create_default_context()  # system trust only
        with pytest.raises(ssl.SSLCertVerificationError):
            ctx.wrap_socket(sock, server_hostname=MITM_HOST)

    await asyncio.to_thread(client)


async def test_tunnels_non_allowlisted_host_untouched(proxy):
    echo = EchoServer()
    await echo.start()
    try:

        def client() -> bytes:
            sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
            sock.sendall(
                f"CONNECT 127.0.0.1:{echo.port} HTTP/1.1\r\n\r\n".encode()
            )
            assert b"200" in _read_headers(sock)
            sock.sendall(b"ping")
            return _read_all(sock)

        assert await asyncio.to_thread(client) == b"PONG:ping"
    finally:
        await echo.stop()


async def test_non_allowlisted_traffic_never_reaches_the_router(proxy, router):
    echo = EchoServer()
    await echo.start()
    try:

        def client() -> None:
            sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
            sock.sendall(f"CONNECT 127.0.0.1:{echo.port} HTTP/1.1\r\n\r\n".encode())
            _read_headers(sock)
            sock.sendall(b"ping")
            _read_all(sock)

        await asyncio.to_thread(client)
        assert router.requests == []
    finally:
        await echo.stop()


async def test_rejects_a_non_connect_request(proxy):
    def client() -> bytes:
        sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
        sock.sendall(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
        return _read_headers(sock)

    assert b"405" in await asyncio.to_thread(client)


async def test_app_lifespan_starts_and_stops_the_proxy(tmp_path, monkeypatch):
    """The wiring itself, not just the class.

    `forward_proxy` is off by default and the lifespan swallows startup errors
    on purpose, so a broken hookup would otherwise show up as Remote Control
    quietly working while `/model qwen` and failover silently stopped — the
    exact pair of symptoms this whole change exists to avoid.
    """
    from src.proxy.app import create_app, lifespan
    from src.proxy.config import clear_settings_cache

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    monkeypatch.setenv("FORWARD_PROXY", "true")
    monkeypatch.setenv("FORWARD_PORT", str(port))
    monkeypatch.setenv("FORWARD_CA_DIR", str(tmp_path / "ca"))
    # The repo's .env may carry a real bot token; process env outranks it.
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    clear_settings_cache()

    def listening() -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                return True
        except OSError:
            return False

    try:
        async with lifespan(create_app()):
            assert await asyncio.to_thread(listening)
        assert not await asyncio.to_thread(listening)
    finally:
        clear_settings_cache()


async def test_reports_an_unreachable_tunnel_target_as_502(proxy):
    # Port 1 on loopback refuses immediately; the proxy must answer rather than
    # hang, or Claude Code sits waiting on a socket that will never open.
    def client() -> bytes:
        sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
        sock.sendall(b"CONNECT 127.0.0.1:1 HTTP/1.1\r\n\r\n")
        return _read_headers(sock)

    assert b"502" in await asyncio.to_thread(client)
