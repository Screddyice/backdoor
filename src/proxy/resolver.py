"""Last-known-good DNS for every outbound call this router makes.

The router's upstreams are addressed by name, and this machine has exactly one
resolver: the LAN gateway handed out by DHCP. When that gateway stops answering
while it keeps routing packets, `getaddrinfo` fails, httpx raises `ConnectError:
[Errno 8] nodename nor servname provided`, and the router has nowhere to send a
request that was otherwise perfectly serviceable.

That is not a rare shape here. Of 731 upstream transport failures in the router
log, 722 are that errno — 98.8%. The link was up for all of them; only name
resolution was gone. The longest measured episode ran 2026-09-04 23:36:32 to
23:39:41.

So a successful lookup is remembered, and a lookup that fails with a resolver
error is answered from that memory instead of raising. The addresses of an API
edge change on a scale of months; a gateway stalls for minutes. Serving the
older answer is right nearly every time, and wrong in a way that is caught
immediately: the connection is still made with TLS verification against the
requested hostname, so a stale address that now belongs to someone else fails
the handshake rather than being trusted.

**This is the layer that should absorb a DNS outage, not the failover breaker.**
`failover.py` is explicit that claiming the local GPU is only justified when it
is the *only* way a session survives — it loads a 17 GB tier into the Ollama
server the llm-jury council wants. A cached address that still connects means
local failover was never the only way, so the session stays on the cloud model
and the GPU stays free. The breaker remains the backstop for when the cached
address fails too.

Set BACKDOOR_DNS_CACHE=0 to disable, leaving bare `socket.getaddrinfo`.
"""

import logging
import os
import socket
import threading
import time

logger = logging.getLogger(__name__)

# How long a remembered answer stays usable. Six hours covers every DNS episode
# in the router log with room to spare, and is far short of the timescale on
# which an upstream actually renumbers.
CACHE_TTL = 6 * 3600.0

# Entries kept before the oldest is dropped. The router talks to a handful of
# hosts; the cap is here so a pathological caller cannot grow this without
# bound, not because the working set is ever near it.
CACHE_MAX = 256

_ENV_DISABLE = "BACKDOOR_DNS_CACHE"

_cache: dict[tuple, tuple[float, list]] = {}
_lock = threading.Lock()
_real_getaddrinfo = None


def enabled() -> bool:
    return os.environ.get(_ENV_DISABLE, "").strip().lower() not in {"0", "false", "no"}


def _cacheable(host) -> bool:
    """Only real names are worth remembering.

    A literal address resolves without a resolver, so caching it would store a
    lookup that cannot fail. `None` and the empty host are the loopback/wildcard
    forms and are equally resolver-free.
    """
    if not host or not isinstance(host, str):
        return False
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, host)
            return False  # already an address
        except OSError:
            continue
    return True


def _remember(key: tuple, result: list) -> None:
    with _lock:
        _cache[key] = (time.monotonic(), result)
        while len(_cache) > CACHE_MAX:
            _cache.pop(next(iter(_cache)))


def _recall(key: tuple) -> list | None:
    with _lock:
        entry = _cache.get(key)
    if entry is None:
        return None
    stored_at, result = entry
    age = time.monotonic() - stored_at
    if age > CACHE_TTL:
        with _lock:
            _cache.pop(key, None)
        return None
    return result


def resolve(host, port, *args, **kwargs):
    """`socket.getaddrinfo`, answered from memory when the resolver will not.

    Every argument is part of the cache key, so a caller asking for a different
    family or flag set never receives another caller's answer.
    """
    assert _real_getaddrinfo is not None, "install() has not run"
    key = (host, port, args, tuple(sorted(kwargs.items())))
    try:
        result = _real_getaddrinfo(host, port, *args, **kwargs)
    except socket.gaierror as exc:
        if not _cacheable(host):
            raise
        remembered = _recall(key)
        if remembered is None:
            raise
        logger.warning(
            "DNS failed for %s (%s) — answering from the last good lookup; the "
            "resolver is down but this host stays reachable",
            host, exc,
        )
        return remembered
    if _cacheable(host):
        _remember(key, result)
    return result


def install() -> bool:
    """Put :func:`resolve` in front of `socket.getaddrinfo` for this process.

    Global on purpose. Every outbound client here — httpx for Anthropic and
    Codex, the connectivity and service probes, memory recall — reaches the
    resolver through the stdlib, and each one is worth keeping alive through a
    gateway stall. Wrapping the one place they share beats threading a resolver
    argument through clients that do not expose one.

    Idempotent, so a reload cannot stack wrappers.
    """
    global _real_getaddrinfo
    if not enabled():
        logger.info("DNS last-known-good cache disabled by %s", _ENV_DISABLE)
        return False
    if _real_getaddrinfo is not None:
        return True
    _real_getaddrinfo = socket.getaddrinfo
    socket.getaddrinfo = resolve
    logger.info(
        "DNS last-known-good cache installed (ttl %.0fs) — a resolver outage "
        "no longer strands a request whose address is already known",
        CACHE_TTL,
    )
    return True


def system_getaddrinfo():
    """The stdlib resolver, whether or not the cache is installed.

    A caller that is *measuring whether DNS works* must never be answered from
    memory: memory is what it is trying to see past.
    :func:`failover.name_resolution_works` is that caller, and handing it a
    cached answer would make the offline gate report a working resolver through
    the exact outage it exists to catch.
    """
    return _real_getaddrinfo or socket.getaddrinfo


def uninstall() -> None:
    """Restore the stdlib resolver and forget everything. For tests."""
    global _real_getaddrinfo
    if _real_getaddrinfo is not None:
        socket.getaddrinfo = _real_getaddrinfo
        _real_getaddrinfo = None
    with _lock:
        _cache.clear()
