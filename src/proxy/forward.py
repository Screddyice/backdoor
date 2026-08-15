"""A CONNECT forward proxy that keeps Remote Control and the router coexisting.

## The problem it solves

Claude Code hides Remote Control whenever `ANTHROPIC_BASE_URL` points anywhere
but `api.anthropic.com` (see `src/proxy/ca.py` for the decompiled check). So the
old arrangement forced a choice: point the variable at :8083 and get `/model
qwen` plus offline failover but no Remote Control, or leave it unset and get
Remote Control with no failover. `claude` took the first, `claude-rc` the
second.

The choice was never real. The router has to be **in the request path**, not
**named as the base URL**. Claude Code honours `HTTPS_PROXY` — verified
2026-08-14 against a logging probe, which recorded `CONNECT api.anthropic.com:443`
while the base URL was unset. Sitting in the proxy slot satisfies both:

    claude   (ANTHROPIC_BASE_URL unset  →  Remote Control offered)
      │  HTTPS_PROXY=127.0.0.1:8084
      ├── CONNECT api.anthropic.com:443 ─► TLS-terminate ─► :8083 router
      └── CONNECT anything else         ─► opaque tunnel

## Why the intercepted leg is a byte splice, not an HTTP proxy

After TLS termination the client speaks plain HTTP/1.1, and so does uvicorn on
:8083. Parsing and re-emitting that in between would mean owning chunked
transfer encoding, keep-alive, and — the one that actually bites — SSE streaming
for `/v1/messages`. Splicing raw bytes gets all three for free and cannot
reframe a response it does not understand. The `Host:` header still reads
`api.anthropic.com`, which is exactly what the router's passthrough already
expects.

The corollary is in `LocalCA.server_ssl_context`: ALPN must offer only
`http/1.1`, because a spliced h2 stream would reach an HTTP/1.1 parser.

## Why everything else is blind

One session's traffic also includes Composio, mem0, Neon, PostHog, npm, and the
Remote Control bridge itself. Those get an opaque TCP tunnel — no certificate is
minted, no plaintext is observed, and the proxy cannot break pinning it never
participates in.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
from typing import Iterable

from .ca import LocalCA

logger = logging.getLogger(__name__)

# A request line plus headers; anything larger is not a proxy client we serve.
_MAX_HEADER_BYTES = 64 * 1024
# A client that opens a socket and sends nothing must not pin a task forever.
_HEADER_TIMEOUT = 30.0
_HANDSHAKE_TIMEOUT = 30.0
_CHUNK = 64 * 1024

_ESTABLISHED = b"HTTP/1.1 200 Connection Established\r\n\r\n"


def _error(status: int, reason: str, detail: str) -> bytes:
    body = detail.encode()
    return (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: text/plain\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode() + body


def _split_authority(target: str) -> tuple[str, int]:
    """Split a CONNECT target into `(host, port)`.

    Handles the bracketed IPv6 form (`[::1]:443`) so a literal address is not
    shredded by the port split.
    """
    if target.startswith("["):
        close = target.find("]")
        if close == -1:
            raise ValueError(f"malformed IPv6 authority: {target!r}")
        host = target[1:close]
        rest = target[close + 1 :]
        port = int(rest[1:]) if rest.startswith(":") else 443
        return host, port

    host, sep, port_text = target.rpartition(":")
    if not sep:
        return target, 443
    return host, int(port_text)


def _close(writer: asyncio.StreamWriter) -> None:
    """Close without awaiting.

    Deliberately synchronous. These calls live in `finally` blocks that run
    while the task is being cancelled, and awaiting there — `await
    writer.wait_closed()` — throws `RuntimeError: coroutine ignored
    GeneratorExit` and leaves the task pending, which is exactly what the
    router logged on every client disconnect before this existed. Closing the
    transport is enough for cleanup; the flush finishes on its own.
    """
    with contextlib.suppress(Exception):
        writer.close()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy one direction until EOF, then half-close the far end."""
    try:
        while True:
            data = await reader.read(_CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, ssl.SSLError, OSError):
        # A peer hanging up mid-stream is ordinary: Claude Code cancels requests,
        # and TLS close_notify races the socket. Nothing here is actionable.
        pass
    finally:
        with contextlib.suppress(Exception):
            if writer.can_write_eof():
                writer.write_eof()


async def _splice(
    a_reader: asyncio.StreamReader,
    a_writer: asyncio.StreamWriter,
    b_reader: asyncio.StreamReader,
    b_writer: asyncio.StreamWriter,
) -> None:
    await asyncio.gather(
        _pipe(a_reader, b_writer),
        _pipe(b_reader, a_writer),
        return_exceptions=True,
    )


class ForwardProxy:
    """HTTP CONNECT proxy: intercepts an allowlist, tunnels the rest."""

    def __init__(
        self,
        *,
        listen_host: str = "127.0.0.1",
        listen_port: int = 8084,
        mitm_hosts: Iterable[str],
        router_host: str = "127.0.0.1",
        router_port: int = 8083,
        ca: LocalCA,
    ) -> None:
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.mitm_hosts = {h.strip().lower() for h in mitm_hosts if h.strip()}
        self.router_host = router_host
        self.router_port = router_port
        self.ca = ca
        self._server: asyncio.AbstractServer | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self.listen_host, self.listen_port
        )
        logger.info(
            "CONNECT forward proxy on %s:%d — intercepting %s → %s:%d",
            self.listen_host,
            self.port,
            ", ".join(sorted(self.mitm_hosts)) or "(nothing)",
            self.router_host,
            self.router_port,
        )

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        with contextlib.suppress(Exception):
            await self._server.wait_closed()
        self._server = None

    @property
    def port(self) -> int:
        """The bound port — resolves `listen_port=0` to what the OS chose."""
        if self._server is None:
            return self.listen_port
        return self._server.sockets[0].getsockname()[1]

    # ── connection handling ──────────────────────────────────────────────────

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await self._dispatch(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionResetError, TimeoutError):
            pass
        except asyncio.CancelledError:
            # Shutdown, or the client went away mid-tunnel. Ordinary; propagate
            # so the loop can finish cancelling rather than logging a fault.
            raise
        except Exception:
            logger.exception("forward proxy connection failed")
        finally:
            _close(writer)

    async def _dispatch(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            head = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=_HEADER_TIMEOUT
            )
        except asyncio.LimitOverrunError:
            writer.write(_error(431, "Request Header Fields Too Large", "headers too large"))
            await writer.drain()
            return

        if len(head) > _MAX_HEADER_BYTES:
            writer.write(_error(431, "Request Header Fields Too Large", "headers too large"))
            await writer.drain()
            return

        request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = request_line.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            # Absolute-form GET/POST is the other half of the proxy spec, but
            # Claude Code only ever issues CONNECT and implementing forward-mode
            # HTTP would add a plaintext path with no caller.
            writer.write(
                _error(405, "Method Not Allowed", "this proxy only supports CONNECT\n")
            )
            await writer.drain()
            return

        try:
            host, port = _split_authority(parts[1])
        except ValueError:
            writer.write(_error(400, "Bad Request", "malformed CONNECT target\n"))
            await writer.drain()
            return

        if host.lower() in self.mitm_hosts:
            await self._intercept(reader, writer, host)
        else:
            await self._tunnel(reader, writer, host, port)

    async def _intercept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, host: str
    ) -> None:
        """TLS-terminate for `host` and splice the plaintext into the router."""
        # Dial the router first: if it is down, say so in plaintext before the
        # client commits to a handshake it would only have to tear down.
        try:
            r_reader, r_writer = await asyncio.open_connection(
                self.router_host, self.router_port
            )
        except OSError as exc:
            logger.warning(
                "router %s:%d unreachable for %s: %s",
                self.router_host,
                self.router_port,
                host,
                exc,
            )
            writer.write(_error(502, "Bad Gateway", f"router unreachable: {exc}\n"))
            await writer.drain()
            return

        try:
            # Mint BEFORE announcing the tunnel. On first use for a host this is
            # a blocking RSA keygen (two, if the CA itself is being created), so
            # it runs off the event loop — and it must not happen between the
            # 200 and `start_tls`, for the reason in the next comment.
            ctx = await asyncio.to_thread(self.ca.server_ssl_context, host)

            # Stop delivering bytes to the plaintext StreamReader before the
            # client is told to start talking TLS.
            #
            # Without this the handshake deadlocks. The moment the client reads
            # `200 Connection Established` it sends a ClientHello; if the
            # transport is still feeding this protocol, those bytes land in the
            # StreamReader's buffer. `loop.start_tls` then installs an
            # SSLProtocol that only sees data arriving *after* it — the
            # ClientHello is already gone, so both ends wait forever and the
            # client times out in `_ssl.c` (observed 2026-08-15).
            #
            # Pausing leaves the ClientHello in the socket's own receive buffer
            # instead, where the SSLProtocol picks it up. `loop.start_tls`
            # resumes reading itself, so there is no matching resume here.
            transport = writer.transport
            leftover = getattr(reader, "_buffer", b"")
            if leftover:
                # Only reachable if a client pipelined payload behind CONNECT,
                # which no HTTP proxy client does. Fail loudly rather than
                # silently drop bytes out of the tunnel.
                raise RuntimeError(
                    f"{len(leftover)} bytes buffered before TLS upgrade for {host}"
                )
            transport.pause_reading()

            writer.write(_ESTABLISHED)
            await writer.drain()

            await asyncio.wait_for(
                writer.start_tls(ctx), timeout=_HANDSHAKE_TIMEOUT
            )

            await _splice(reader, writer, r_reader, r_writer)
        except (ssl.SSLError, ConnectionResetError, TimeoutError) as exc:
            # A client that does not trust our CA aborts here. Expected whenever
            # something other than the configured launcher hits the proxy.
            logger.info("TLS interception for %s ended early: %s", host, exc)
        finally:
            _close(r_writer)

    async def _tunnel(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        host: str,
        port: int,
    ) -> None:
        """Opaque relay — no certificate, no plaintext, no inspection."""
        try:
            t_reader, t_writer = await asyncio.open_connection(host, port)
        except OSError as exc:
            writer.write(_error(502, "Bad Gateway", f"cannot reach {host}:{port}: {exc}\n"))
            await writer.drain()
            return

        try:
            writer.write(_ESTABLISHED)
            await writer.drain()
            await _splice(reader, writer, t_reader, t_writer)
        finally:
            _close(t_writer)
