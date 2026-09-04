"""Tests for the CONNECT forward proxy.

The proxy exists so `ANTHROPIC_BASE_URL` can stay unset — that variable is what
Claude Code checks before offering Remote Control — while the router still sees
every request. It does that by sitting in `HTTPS_PROXY` instead.

Two behaviours carry the whole design and are tested here:

1. **Allowlisted hosts are intercepted.** `api.anthropic.com` is TLS-terminated
   with a locally minted leaf and the plaintext is spliced into the router, so
   `/model qwen` routing and offline failover keep working.

2. **Everything else is tunnelled blind.** A single `claude` session also talks
   to Composio, Neon, PostHog, npm and, most importantly, the Remote Control
   bridge on claude.ai. Intercepting any of those would break certificate
   pinning at best and silently reroute traffic at worst. They must pass through
   as opaque bytes.

Client work runs in threads via `asyncio.to_thread`: blocking sockets express
"CONNECT, then upgrade this same socket to TLS" far more directly than
`loop.start_tls`, and the clarity matters more than the thread here.
"""

import asyncio
import logging
import socket
import ssl
import struct
import time
from unittest.mock import MagicMock

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
        except (ssl.SSLError, ConnectionResetError):
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


class HalfCloseServer:
    """A peer that responds after EOF or stays silent until released."""

    def __init__(self, response: bytes | None, *, delay: float = 0.0) -> None:
        self.response = response
        self.delay = delay
        self._server: asyncio.AbstractServer | None = None
        self._release = asyncio.Event()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    async def _handle(self, reader, writer) -> None:
        try:
            await reader.read()
            if self.response is None:
                await self._release.wait()
            else:
                await asyncio.sleep(self.delay)
                writer.write(self.response)
                await writer.drain()
        finally:
            writer.close()

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self._release.set()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


class HeartbeatServer:
    """Send enough downstream activity to outlive an absolute timeout."""

    def __init__(self) -> None:
        self._server: asyncio.AbstractServer | None = None
        self._release = asyncio.Event()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    async def _handle(self, reader, writer) -> None:
        try:
            for _ in range(4):
                await asyncio.sleep(0.05)
                writer.write(b"x")
                await writer.drain()
            await self._release.wait()
        finally:
            writer.close()

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self._release.set()
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


async def test_tunnel_preserves_half_close_for_a_delayed_response(tmp_path, router):
    peer = HalfCloseServer(b"response-after-eof", delay=0.03)
    await peer.start()
    proxy = ForwardProxy(
        listen_host="127.0.0.1",
        listen_port=0,
        mitm_hosts={MITM_HOST},
        router_host="127.0.0.1",
        router_port=router.port,
        ca=LocalCA(tmp_path / "ca"),
        idle_timeout=0.5,
    )
    await proxy.start()
    try:
        def client() -> bytes:
            sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=1)
            sock.sendall(f"CONNECT 127.0.0.1:{peer.port} HTTP/1.1\r\n\r\n".encode())
            assert b"200" in _read_headers(sock)
            sock.sendall(b"request")
            sock.shutdown(socket.SHUT_WR)
            return _read_all(sock)

        assert await asyncio.to_thread(client) == b"response-after-eof"
    finally:
        await proxy.stop()
        await peer.stop()


async def test_tunnel_releases_a_silent_peer_after_idle_timeout(tmp_path, router):
    peer = HalfCloseServer(None)
    await peer.start()
    proxy = ForwardProxy(
        listen_host="127.0.0.1",
        listen_port=0,
        mitm_hosts={MITM_HOST},
        router_host="127.0.0.1",
        router_port=router.port,
        ca=LocalCA(tmp_path / "ca"),
        idle_timeout=0.05,
    )
    await proxy.start()
    try:
        def client() -> bytes:
            sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=1)
            sock.sendall(f"CONNECT 127.0.0.1:{peer.port} HTTP/1.1\r\n\r\n".encode())
            assert b"200" in _read_headers(sock)
            sock.sendall(b"request")
            sock.shutdown(socket.SHUT_WR)
            return sock.recv(1)

        assert await asyncio.to_thread(client) == b""
    finally:
        await proxy.stop()
        await peer.stop()


async def test_upstream_bytes_reset_the_shared_idle_deadline(tmp_path, router):
    peer = HalfCloseServer(None)
    await peer.start()
    proxy = ForwardProxy(
        listen_host="127.0.0.1",
        listen_port=0,
        mitm_hosts={MITM_HOST},
        router_host="127.0.0.1",
        router_port=router.port,
        ca=LocalCA(tmp_path / "ca"),
        idle_timeout=0.15,
    )
    await proxy.start()
    try:
        def client() -> bytes:
            sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=1)
            sock.sendall(f"CONNECT 127.0.0.1:{peer.port} HTTP/1.1\r\n\r\n".encode())
            assert b"200" in _read_headers(sock)
            for _ in range(4):
                sock.sendall(b"x")
                sock.settimeout(0.02)
                with pytest.raises(socket.timeout):
                    sock.recv(1)
                time.sleep(0.03)
            sock.settimeout(1)
            return sock.recv(1)

        assert await asyncio.to_thread(client) == b""
    finally:
        await proxy.stop()
        await peer.stop()


async def test_downstream_bytes_reset_the_shared_idle_deadline(tmp_path, router):
    peer = HeartbeatServer()
    await peer.start()
    proxy = ForwardProxy(
        listen_host="127.0.0.1",
        listen_port=0,
        mitm_hosts={MITM_HOST},
        router_host="127.0.0.1",
        router_port=router.port,
        ca=LocalCA(tmp_path / "ca"),
        idle_timeout=0.15,
    )
    await proxy.start()
    try:
        def client() -> tuple[bytes, bytes]:
            sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=1)
            sock.sendall(f"CONNECT 127.0.0.1:{peer.port} HTTP/1.1\r\n\r\n".encode())
            assert b"200" in _read_headers(sock)
            received = b""
            while len(received) < 4:
                received += sock.recv(4 - len(received))
            return received, sock.recv(1)

        assert await asyncio.to_thread(client) == (b"xxxx", b"")
    finally:
        await proxy.stop()
        await peer.stop()


async def test_rejects_connections_above_the_active_tunnel_cap(tmp_path, router):
    peer = HalfCloseServer(None)
    await peer.start()
    proxy = ForwardProxy(
        listen_host="127.0.0.1",
        listen_port=0,
        mitm_hosts={MITM_HOST},
        router_host="127.0.0.1",
        router_port=router.port,
        ca=LocalCA(tmp_path / "ca"),
        idle_timeout=10,
        max_connections=2,
    )
    await proxy.start()
    clients: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
    try:
        for _ in range(2):
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(f"CONNECT 127.0.0.1:{peer.port} HTTP/1.1\r\n\r\n".encode())
            await writer.drain()
            assert b"200" in await reader.readuntil(b"\r\n\r\n")
            clients.append((reader, writer))

        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        writer.write(f"CONNECT 127.0.0.1:{peer.port} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        assert b"503" in await reader.readuntil(b"\r\n\r\n")
        writer.close()
    finally:
        for _, writer in clients:
            writer.close()
        await proxy.stop()
        await peer.stop()


async def test_stop_closes_active_tunnels_without_waiting_for_idle_timeout(
    tmp_path, router
):
    peer = HalfCloseServer(None)
    await peer.start()
    proxy = ForwardProxy(
        listen_host="127.0.0.1",
        listen_port=0,
        mitm_hosts={MITM_HOST},
        router_host="127.0.0.1",
        router_port=router.port,
        ca=LocalCA(tmp_path / "ca"),
        idle_timeout=10,
    )
    await proxy.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
    try:
        writer.write(f"CONNECT 127.0.0.1:{peer.port} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        assert b"200" in await reader.readuntil(b"\r\n\r\n")

        await asyncio.wait_for(proxy.stop(), timeout=0.2)

        assert await asyncio.wait_for(reader.read(), timeout=0.2) == b""
        assert proxy._connections == set()
    finally:
        writer.close()
        await proxy.stop()
        await peer.stop()


async def test_accept_registers_handler_before_the_event_loop_can_run(
    tmp_path, router
):
    proxy = ForwardProxy(
        listen_host="127.0.0.1",
        listen_port=0,
        mitm_hosts={MITM_HOST},
        router_host="127.0.0.1",
        router_port=router.port,
        ca=LocalCA(tmp_path / "ca"),
    )
    reader = asyncio.StreamReader()
    writer = MagicMock(spec=asyncio.StreamWriter)

    proxy._accept(reader, writer)

    assert len(proxy._connections) == 1
    await asyncio.wait_for(proxy.stop(), timeout=0.2)
    assert proxy._connections == set()
    writer.close.assert_called_once()


async def test_eof_does_not_extend_the_byte_idle_deadline(tmp_path, router):
    peer = HalfCloseServer(None)
    await peer.start()
    proxy = ForwardProxy(
        listen_host="127.0.0.1",
        listen_port=0,
        mitm_hosts={MITM_HOST},
        router_host="127.0.0.1",
        router_port=router.port,
        ca=LocalCA(tmp_path / "ca"),
        idle_timeout=0.3,
    )
    await proxy.start()
    try:
        def client() -> bytes:
            sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=1)
            sock.sendall(f"CONNECT 127.0.0.1:{peer.port} HTTP/1.1\r\n\r\n".encode())
            assert b"200" in _read_headers(sock)
            sock.sendall(b"request")
            time.sleep(0.25)
            sock.shutdown(socket.SHUT_WR)
            sock.settimeout(0.15)
            return sock.recv(1)

        assert await asyncio.to_thread(client) == b""
    finally:
        await proxy.stop()
        await peer.stop()


# ── Telling CA distrust apart from pool churn ────────────────────────────────
# `_intercept` caught ssl.SSLError, ConnectionResetError and TimeoutError in one
# clause and logged all three as `TLS interception for <host> ended early`, with
# a comment attributing it to a client that does not trust the CA. The log said
# otherwise: over one 8-day rotation, 33,092 ConnectionResetError, 465
# TimeoutError and 25 TLSV1_ALERT_UNKNOWN_CA. The rare one is the real fault and
# the frequent one is Claude Code's and Codex's connection pools opening a
# CONNECT tunnel and dropping it before TLS. Logging both per occurrence spent
# about a third of a 10 MB rotation on the benign one and buried the other 25.


async def test_a_client_that_rejects_the_ca_is_warned_about_by_name(proxy, caplog):
    def client() -> None:
        sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
        sock.sendall(f"CONNECT {MITM_HOST}:443 HTTP/1.1\r\n\r\n".encode())
        _read_headers(sock)
        ctx = ssl.create_default_context()  # system trust only
        with pytest.raises(ssl.SSLCertVerificationError):
            ctx.wrap_socket(sock, server_hostname=MITM_HOST)

    with caplog.at_level(logging.DEBUG, logger="src.proxy.forward"):
        await asyncio.to_thread(client)
        for _ in range(100):
            if any(r.levelno >= logging.WARNING for r in caplog.records):
                break
            await asyncio.sleep(0.02)

    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.name == "src.proxy.forward" and r.levelno >= logging.WARNING
    ]
    assert len(warnings) == 1
    assert "CA" in warnings[0]
    assert MITM_HOST in warnings[0]


async def test_a_tunnel_dropped_before_tls_is_counted_not_logged(proxy, caplog):
    def client() -> None:
        sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
        sock.sendall(f"CONNECT {MITM_HOST}:443 HTTP/1.1\r\n\r\n".encode())
        _read_headers(sock)
        # RST rather than FIN, which is how a pooled socket goes away.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        sock.close()

    with caplog.at_level(logging.DEBUG, logger="src.proxy.forward"):
        await asyncio.to_thread(client)
        for _ in range(100):
            if proxy.abandoned_tunnels:
                break
            await asyncio.sleep(0.02)

    assert proxy.abandoned_tunnels == 1
    # Scoped to the proxy's own logger: dropping the client also drops the
    # router leg `_intercept` had already dialled, and the StubRouter here
    # complains about that on the asyncio logger.
    assert [r.getMessage() for r in caplog.records if r.name == "src.proxy.forward"] == []


async def test_abandoned_tunnels_are_summarised_once_per_interval(caplog):
    clock = [1000.0]
    proxy = ForwardProxy(
        listen_host="127.0.0.1",
        listen_port=0,
        mitm_hosts={MITM_HOST},
        router_host="127.0.0.1",
        router_port=1,
        ca=MagicMock(),
        now=lambda: clock[0],
        abandon_summary_interval=300.0,
    )

    with caplog.at_level(logging.DEBUG, logger="src.proxy.forward"):
        for _ in range(3):
            proxy._note_abandoned_tunnel(MITM_HOST, ConnectionResetError(54, "reset"))
        assert [r for r in caplog.records if r.name == "src.proxy.forward"] == []
        clock[0] += 301.0
        proxy._note_abandoned_tunnel(MITM_HOST, TimeoutError())

    messages = [r.getMessage() for r in caplog.records if r.name == "src.proxy.forward"]
    assert len(messages) == 1
    assert "4" in messages[0]
    assert "3 reset" in messages[0]
    assert "1 handshake timeout" in messages[0]
    assert proxy.abandoned_tunnels == 0
