"""Unit tests for the last-known-good DNS cache.

Nothing here touches a real resolver: `socket.getaddrinfo` is replaced with a
fake whose failures are scripted, which is the only way to test the case that
matters — a lookup that used to work and now raises.
"""

import socket

import pytest

from src.proxy import resolver

_ANSWER = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("160.79.104.10", 443))]
_OTHER = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 443))]


class FakeResolver:
    """Answers until `broken` is set, then fails the way a dead gateway does."""

    def __init__(self, answer=_ANSWER):
        self.answer = answer
        self.broken = False
        self.calls = 0

    def __call__(self, host, port, *a, **kw):
        self.calls += 1
        if self.broken:
            raise socket.gaierror(8, "nodename nor servname provided, or not known")
        return self.answer


@pytest.fixture
def fake(monkeypatch):
    resolver.uninstall()  # an app-lifespan test may have installed the real one
    f = FakeResolver()
    monkeypatch.setattr(socket, "getaddrinfo", f)
    resolver.install()
    yield f
    resolver.uninstall()


def test_a_live_lookup_is_passed_straight_through(fake):
    assert resolver.resolve("api.anthropic.com", 443) == _ANSWER
    assert fake.calls == 1, "a working resolver must not be shadowed by the cache"


def test_a_failed_lookup_is_answered_from_the_last_good_one(fake):
    """The 2026-09-04 outage: the address was known, the resolver was not."""
    resolver.resolve("api.anthropic.com", 443)
    fake.broken = True

    assert resolver.resolve("api.anthropic.com", 443) == _ANSWER


def test_a_failure_with_nothing_remembered_still_raises(fake):
    """Inventing an address is worse than failing. Never guess."""
    fake.broken = True

    with pytest.raises(socket.gaierror):
        resolver.resolve("never-looked-up.example", 443)


def test_a_fresh_answer_replaces_the_remembered_one(fake):
    resolver.resolve("api.anthropic.com", 443)
    fake.answer = _OTHER
    resolver.resolve("api.anthropic.com", 443)
    fake.broken = True

    assert resolver.resolve("api.anthropic.com", 443) == _OTHER, (
        "the cache must follow a renumbering, not pin the first answer forever"
    )


def test_a_stale_answer_expires(fake, monkeypatch):
    """Past the TTL the cache stops speaking for a host it no longer knows."""
    clock = [0.0]
    monkeypatch.setattr(resolver.time, "monotonic", lambda: clock[0])
    resolver.resolve("api.anthropic.com", 443)   # remembered at t=0
    fake.broken = True
    clock[0] = resolver.CACHE_TTL + 1.0

    with pytest.raises(socket.gaierror):
        resolver.resolve("api.anthropic.com", 443)


def test_different_call_shapes_do_not_share_an_answer(fake):
    """A caller asking for AF_INET6 must not be handed the AF_INET reply."""
    resolver.resolve("api.anthropic.com", 443, socket.AF_INET)
    fake.broken = True

    with pytest.raises(socket.gaierror):
        resolver.resolve("api.anthropic.com", 443, socket.AF_INET6)


def test_literal_addresses_are_never_cached(fake):
    """A literal needs no resolver, so a cache entry for one could only mislead."""
    resolver.resolve("1.1.1.1", 443)
    fake.broken = True

    with pytest.raises(socket.gaierror):
        resolver.resolve("1.1.1.1", 443)


def test_install_is_idempotent(fake):
    """A reload must not stack wrappers, or every lookup pays for each layer."""
    wrapped = socket.getaddrinfo
    resolver.install()

    assert socket.getaddrinfo is wrapped


def test_the_env_switch_leaves_the_stdlib_alone(monkeypatch):
    resolver.uninstall()
    monkeypatch.setenv("BACKDOOR_DNS_CACHE", "0")
    original = socket.getaddrinfo

    assert resolver.install() is False
    assert socket.getaddrinfo is original


def test_the_dns_probe_is_never_answered_from_the_cache(fake):
    """The failover gate asks whether DNS works; memory cannot answer that.

    Without this, the cache would report a healthy resolver through the very
    outage the offline gate exists to detect, and the breaker would stay shut
    for a second reason.
    """
    from src.proxy.failover import name_resolution_works

    resolver.resolve("one.one.one.one", 443)
    fake.broken = True

    assert name_resolution_works(timeout=1.0) is False
