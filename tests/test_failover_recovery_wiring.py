"""The recovery ticker must run for the whole life of the router.

The breaker's half-open path needs a real `/v1/messages` passthrough to carry it
upstream, and an outage is precisely what stops those arriving: a session served
by a local tier generates far less traffic, and an abandoned one none at all. So
recovery cannot be left to depend on traffic. Measured 2026-09-02 — the breaker
was OPEN from 22:28:14 to 23:45:51, 77 minutes 37 seconds, most of it on a
network that had already recovered, and it closed at the exact second unrelated
test traffic reached it. Every routed session spent that window silently on a
local model.

These cover the two halves that make the ticker real: the loop's own behaviour,
and the lifespan actually starting and stopping it.
"""

import asyncio

import pytest

import src.proxy.routes as routes
from src.proxy.app import lifespan
from src.proxy.config import Settings


class _Breaker:
    """Just enough breaker for the loop: open/closed plus a scripted recovery."""

    def __init__(self, open_: bool, recovers: bool):
        self.open = open_
        self._recovers = recovers
        self.probes = 0

    def maybe_recover(self) -> bool:
        self.probes += 1
        if self._recovers:
            self.open = False
        return self._recovers


class _Stop(Exception):
    """Breaks the loop's `while True` from inside the injected sleep."""


def _run_loop(monkeypatch, breaker, ticks: int, released: list):
    monkeypatch.setattr(routes, "get_breaker", lambda settings: breaker)

    async def release(br, settings):
        released.append(br)

    monkeypatch.setattr(routes, "_release_claims", release)

    remaining = [ticks]

    async def sleep(_interval):
        if remaining[0] <= 0:
            raise _Stop
        remaining[0] -= 1

    async def drive():
        with pytest.raises(_Stop):
            await routes.failover_recovery_loop(Settings(), sleep=sleep)

    asyncio.run(drive())


def test_the_ticker_does_nothing_while_the_breaker_is_closed(monkeypatch):
    """Essentially always. The probe is real socket work; it must stay unpaid for."""
    breaker = _Breaker(open_=False, recovers=True)
    released = []
    _run_loop(monkeypatch, breaker, ticks=5, released=released)
    assert breaker.probes == 0
    assert released == []


def test_the_ticker_closes_an_open_breaker_and_releases_its_tiers(monkeypatch):
    """Closing is the exact moment the GPU is no longer needed.

    Leaving the tiers resident is what kept ~9.7 GB wired past the close on
    2026-08-24, so a recovery that closes without releasing would reintroduce it.
    """
    breaker = _Breaker(open_=True, recovers=True)
    released = []
    _run_loop(monkeypatch, breaker, ticks=3, released=released)
    assert breaker.probes == 1, "one probe closed it; the rest are no-ops"
    assert released == [breaker]
    assert not breaker.open


def test_the_ticker_keeps_probing_while_the_host_is_still_offline(monkeypatch):
    breaker = _Breaker(open_=True, recovers=False)
    released = []
    _run_loop(monkeypatch, breaker, ticks=3, released=released)
    assert breaker.probes == 3
    assert released == [], "nothing recovered, so nothing to hand back"
    assert breaker.open


def test_a_failing_probe_does_not_kill_the_ticker(monkeypatch):
    """A ticker that dies on one bad probe is a breaker that stays open forever —
    the failure this whole mechanism exists to end."""
    class Exploding(_Breaker):
        def maybe_recover(self):
            self.probes += 1
            raise OSError("probe blew up")

    breaker = Exploding(open_=True, recovers=False)
    _run_loop(monkeypatch, breaker, ticks=3, released=[])
    assert breaker.probes == 3


# ── Lifespan wiring ──────────────────────────────────────────────────────────


def _lifespan_task(monkeypatch, failover_to_local: bool):
    started = asyncio.Event()
    cancelled = []

    async def fake_loop(settings, sleep=None):
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    settings = Settings(failover_to_local=failover_to_local, telegram_bot_token="", forward_proxy=False)
    monkeypatch.setattr("src.proxy.app.failover_recovery_loop", fake_loop)
    monkeypatch.setattr("src.proxy.app.get_settings", lambda: settings)

    async def drive():
        async with lifespan(object()):
            await asyncio.sleep(0)
        return started.is_set()

    return asyncio.run(drive()), cancelled


def test_lifespan_starts_the_ticker_and_cancels_it_on_shutdown(monkeypatch):
    was_started, cancelled = _lifespan_task(monkeypatch, failover_to_local=True)
    assert was_started, "the ticker must be running for the life of the router"
    assert cancelled == [True], "and must not outlive it"


def test_lifespan_skips_the_ticker_when_failover_is_off(monkeypatch):
    was_started, cancelled = _lifespan_task(monkeypatch, failover_to_local=False)
    assert not was_started
    assert cancelled == []


# ── Both breakers, not just Claude's ─────────────────────────────────────────
#
# This router owns two independent breakers — the Claude one in routes and the
# Codex one in codex_routes — with separate thresholds, separate state and
# separate tier-release paths. A ticker wired to only the first would leave the
# second with the exact bug this change exists to remove, so the default target
# list covers both.


def test_the_default_targets_cover_both_breakers(monkeypatch):
    import src.proxy.codex_routes as codex_routes

    claude = _Breaker(open_=True, recovers=True)
    codex = _Breaker(open_=True, recovers=True)
    released = []

    monkeypatch.setattr(routes, "get_breaker", lambda settings: claude)
    monkeypatch.setattr(codex_routes, "get_codex_breaker", lambda settings: codex)

    async def release_claude(br, settings):
        released.append(("claude", br))

    async def release_codex(br):
        released.append(("codex", br))

    monkeypatch.setattr(routes, "_release_claims", release_claude)
    monkeypatch.setattr(codex_routes, "_release_claims", release_codex)

    remaining = [2]

    async def sleep(_interval):
        if remaining[0] <= 0:
            raise _Stop
        remaining[0] -= 1

    async def drive():
        with pytest.raises(_Stop):
            await routes.failover_recovery_loop(Settings(), sleep=sleep)

    asyncio.run(drive())

    assert claude.probes == 1 and codex.probes == 1
    assert [name for name, _ in released] == ["claude", "codex"], (
        "each breaker must be released through its own path"
    )


def test_the_codex_breaker_is_built_with_a_probe_for_its_own_upstream(monkeypatch):
    """The Codex breaker must be pointed at Codex, not at the internet.

    Wiring it to the shared connectivity probe would let a working network close
    a breaker that opened because ChatGPT specifically was unreachable — the
    false-close mirror of the false-open this branch is about.
    """
    import src.proxy.codex_routes as codex_routes

    asked = []
    monkeypatch.setattr(
        codex_routes, "service_reachable", lambda url: asked.append(url) or True
    )
    monkeypatch.setattr(codex_routes, "_codex_breaker", None)

    settings = Settings()
    breaker = codex_routes.get_codex_breaker(settings)
    try:
        assert breaker._service is not None, "a service-level breaker needs a probe"
        assert breaker._service() is True
        assert asked == [settings.codex_chatgpt_upstream]
        assert breaker.require_offline is False, (
            "Codex fails over on service errors, not only on an offline host"
        )
    finally:
        codex_routes._codex_breaker = None


def test_the_ticker_recovers_the_codex_breaker_through_its_service_probe(monkeypatch):
    """End to end: transport-error open, host comes back, ticker closes it."""
    import src.proxy.codex_routes as codex_routes

    monkeypatch.setattr(codex_routes, "_codex_breaker", None)
    reachable = [False]
    monkeypatch.setattr(codex_routes, "service_reachable", lambda url: reachable[0])

    settings = Settings()
    codex = codex_routes.get_codex_breaker(settings)
    released = []

    async def release_codex(br):
        released.append(br)

    monkeypatch.setattr(codex_routes, "_release_claims", release_codex)
    monkeypatch.setattr(
        routes, "get_breaker", lambda s: _Breaker(open_=False, recovers=False)
    )

    try:
        for _ in range(settings.codex_failover_threshold):
            codex.record_failure("ConnectTimeout")
        assert codex.open, "consecutive transport failures open the Codex breaker"

        reachable[0] = True                      # ChatGPT is back
        # This breaker runs on the real clock (get_codex_breaker takes no
        # now_fn), and opening stamps the recovery timer, so wind it back rather
        # than sleeping a whole probe interval in a unit test.
        codex._last_recovery_at -= settings.codex_failover_probe_seconds + 1

        remaining = [2]

        async def sleep(_interval):
            if remaining[0] <= 0:
                raise _Stop
            remaining[0] -= 1

        async def drive():
            with pytest.raises(_Stop):
                await routes.failover_recovery_loop(settings, sleep=sleep)

        asyncio.run(drive())

        assert not codex.open, "the ticker closed it with no Codex request at all"
        assert released == [codex]
    finally:
        codex_routes._codex_breaker = None
