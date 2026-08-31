# Zero-downtime router restarts via launchd socket activation

Date: 2026-08-31 · Status: approved (Shawn, in-session) · Scope: restarts invisible; deploys stay manual

## Problem

Every user-visible outage of this router has been a restart. SIGKILL truncates
in-flight streams ("Connection lost mid-response"). Even a graceful SIGTERM
closes the listen socket at the start of the drain, and launchd only respawns
after exit — one long SSE stream held the port dark ~90s on 2026-08-31 while
every session saw `[Errno 61] Connection refused` (retry `attempt 5/10`).
Bounded drain (20s) and idle-aware `bd-restart` shrank the window; a non-idle
restart still refuses connections for 2–5s.

## Decision

launchd owns the listen sockets. The LaunchAgent declares them under a
`Sockets` key ("api" → 127.0.0.1:8083, "forward" → 127.0.0.1:8084); launchd
creates them once and keeps its own reference open forever. The process
fetches duplicated fds at startup via `launch_activate_socket(3)`
(`src/proxy/socket_activation.py`, ctypes into libSystem, stable ABI since
macOS 10.10) and serves on them: uvicorn via `fd=` (the socket is `detach()`ed
so uvicorn is sole owner), the CONNECT forward proxy via
`asyncio.start_server(sock=...)`.

While the process is down or draining, new connections queue in the
still-open listen backlog and the replacement accepts them seconds later.
Nothing is refused in any phase. A stray manually launched instance cannot
receive activated sockets, falls back to a normal bind, hits EADDRINUSE
against launchd's listener, and dies loudly — making the doomed-second-
instance state-file clobber structurally impossible on top of the existing
ownership guard.

## Fail-open contract

`activated_socket(name)` returns None on ANY non-clean outcome — not launched
by launchd, name missing from the plist, non-Darwin, ctypes surprise — and
every caller then binds host/port exactly as before. Dev runs, tests, and CI
never notice the module.

## Rejected alternatives

- SO_REUSEPORT blue/green: zero queuing, but forces `KeepAlive` off and moves
  lifecycle ownership from launchd into a script.
- Front proxy (true HA): a permanent extra hop and a new always-on component
  that itself needs restarts.

## Adoption / rollback

Adopt: sync the service checkout, add `Sockets` to the live plist, then one
final visible restart (`bootout` + `bootstrap`, idle-aware). Rollback: remove
the key, bootout/bootstrap — the fallback path IS the previous behavior.

## Acceptance

A probe loop (5/s against /health) running through a `launchctl kill TERM`
restart records ZERO connection-refused — only elevated latency — while the
new process picks up queued connections. Unit tests: activation fails open
outside launchd; ForwardProxy serves on an injected socket.
