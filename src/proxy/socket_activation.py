"""launchd socket activation: receive listen sockets instead of binding them.

Why this exists: every user-visible outage of this router has been a restart.
When the process binds its own sockets, the listeners die with it — a SIGKILL
truncates streams, and even a graceful drain closes the listen socket at the
start of the drain, so clients get "[Errno 61] Connection refused" until
launchd notices the exit and the replacement binds (observed ~90s behind one
long SSE stream on 2026-08-31).

With a `Sockets` key in the LaunchAgent, launchd creates the listen sockets
once and keeps ITS OWN reference open forever. The process fetches duplicated
FDs at startup via launch_activate_socket(3). When the process exits — crash,
deploy, drain — connections queue in the still-open listen backlog and the
replacement accepts them seconds later. Nothing is refused, in any phase.

A structural bonus: only launchd can hand out these sockets. A stray manually
launched instance falls back to a normal bind, gets EADDRINUSE against
launchd's socket, and dies loudly instead of half-starting.

Fail-open by design. Anything short of a clean activation — not launched by
launchd, name not in the plist, non-Darwin host, ABI surprise — returns None
and the caller binds host/port exactly as before. Tests and dev runs never
notice this module.
"""

import ctypes
import logging
import socket

logger = logging.getLogger(__name__)


def activated_socket(name: str) -> "socket.socket | None":
    """The launchd-provided listen socket registered under `name`, or None.

    launch_activate_socket(3) is in libSystem (stable ABI since macOS 10.10):

        int launch_activate_socket(const char *name, int **fds, size_t *cnt);

    Returns 0 and a malloc'd fd array on success; ENOENT when this process was
    not launched by launchd with that socket name; ESRCH outside launchd. The
    array must be free(3)d; the fds themselves are ours to keep.

    The plist declares one concrete address per name (SockNodeName pins IPv4
    loopback), so one fd comes back. If a future plist ever yields more, the
    first is used and the rest are closed rather than leaked.
    """
    try:
        lib = ctypes.CDLL(None)
        fn = lib.launch_activate_socket
        free = lib.free
    except (OSError, AttributeError):
        return None  # non-Darwin, or a libSystem without the symbol

    fn.restype = ctypes.c_int
    fn.argtypes = [
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_int)),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    free.restype = None
    free.argtypes = [ctypes.c_void_p]

    fds = ctypes.POINTER(ctypes.c_int)()
    count = ctypes.c_size_t(0)
    try:
        rc = fn(name.encode("utf-8"), ctypes.byref(fds), ctypes.byref(count))
    except Exception:  # ctypes-level surprise: fail open, never crash startup
        logger.exception("launch_activate_socket call failed for %r", name)
        return None
    if rc != 0 or count.value == 0:
        return None

    try:
        # socket.socket(fileno=) adopts the fd (no dup) and auto-detects
        # family/type via getsockopt, so the object is a real listening socket.
        primary = socket.socket(fileno=fds[0])
        for i in range(1, count.value):
            try:
                socket.socket(fileno=fds[i]).close()
            except OSError:
                pass
    finally:
        free(fds)

    logger.info(
        "socket activation: %r from launchd on %s (restarts queue instead of refuse)",
        name,
        primary.getsockname(),
    )
    return primary
