"""A blip inside the breaker's `min_outage` gate must not reach the client as 502.

Regression coverage for the 2026-09-08 00:50 / 01:00 stall. `min_outage` makes
the breaker wait out a brief outage before claiming the GPU, and every request
that failed inside that gate was answered with a bare 502. Claude Code treats a
502 as "retry later" with escalating backoff, so seven consecutive 502s in the
20s before the breaker opened at 00:51:04 put the session into a `will retry in
2m 29s` countdown — still running at 01:01:24, thirteen seconds after the second
outage had fully closed. The router recovered in 26 seconds; the session did
not recover for minutes.

`_try_upstream` now holds a turn while the verdict is pending and both exits
serve a real answer: upstream returns, or the breaker opens and the turn goes
local. The 502 survives only for a verdict the breaker actually delivered.
"""

import asyncio
import time

import httpx
import pytest

import src.proxy.routes as routes
from src.proxy.config import Settings
from src.proxy.failover import FailoverBreaker


class _Req:
    """Minimal stand-in for the Request bits `_upstream_send` reads."""

    def __init__(self):
        self.method = "POST"
        self.headers = {"content-type": "application/json"}
        self.url = httpx.URL("http://127.0.0.1:8083/v1/messages")


class FlakyUpstream:
    """Fails `fail_times` sends, then succeeds."""

    def __init__(self, fail_times, exc=httpx.ConnectTimeout("timed out")):
        self.remaining = fail_times
        self.exc = exc
        self.sends = 0

    def build_request(self, method, url, *, content, headers):
        return httpx.Request(method, f"https://api.anthropic.com{url}", content=content, headers=headers)

    async def send(self, request, *, stream):
        self.sends += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise self.exc
        return httpx.Response(200, request=request, content=b"{}")

    async def aclose(self):
        pass


@pytest.fixture(autouse=True)
def _fast_hold(monkeypatch):
    """Keep the cadence real in shape but instant in wall-clock."""
    monkeypatch.setattr(routes, "_HOLD_RETRY_SECONDS", 0.0)


@pytest.fixture(autouse=True)
def _reset():
    yield
    routes._upstream_client = None
    routes._breaker = None


def _wire(monkeypatch, upstream, breaker):
    routes._upstream_client = upstream
    monkeypatch.setattr(routes, "_get_upstream", lambda settings: upstream)
    monkeypatch.setattr(routes, "get_breaker", lambda settings: breaker)


def _breaker(**kw):
    kw.setdefault("min_outage", 20.0)
    kw.setdefault("threshold", 1)
    kw.setdefault("require_offline", False)
    # Never actually open in the tests that only exercise the hold.
    kw.setdefault("online_fn", lambda: True)
    return FailoverBreaker(**kw)


@pytest.mark.asyncio
async def test_blip_inside_the_gate_is_held_not_502(monkeypatch):
    """The turn waits out a short blip and relays the real response."""
    clock = {"t": 0.0}
    monkeypatch.setattr(routes.time, "monotonic", lambda: clock["t"])
    # Two failures, then upstream comes back — the 2026-09-08 01:00 shape.
    upstream = FlakyUpstream(fail_times=2)
    br = _breaker(now_fn=lambda: clock["t"])
    _wire(monkeypatch, upstream, br)

    resp = await routes._try_upstream(_Req(), b"{}", Settings(router_mode="hybrid"))

    assert resp is not None, "a blip inside the gate must not fail the turn"
    assert resp.status_code == 200
    assert upstream.sends == 3, "held turn retried upstream rather than 502-ing"


@pytest.mark.asyncio
async def test_hold_ends_when_another_request_opens_the_breaker(monkeypatch):
    """A breaker opened by a peer sends the held turn local, not to a 502."""
    clock = {"t": 0.0}
    monkeypatch.setattr(routes.time, "monotonic", lambda: clock["t"])
    upstream = FlakyUpstream(fail_times=99)
    br = _breaker(now_fn=lambda: clock["t"])
    _wire(monkeypatch, upstream, br)

    real_sleep = routes.asyncio.sleep

    async def open_it(_):
        br.open = True  # a peer request tripped it while this one waited
        await real_sleep(0)

    monkeypatch.setattr(routes.asyncio, "sleep", open_it)

    resp = await routes._try_upstream(_Req(), b"{}", Settings(router_mode="hybrid"))

    assert resp is None, "an open breaker means serve locally, never 502"


@pytest.mark.asyncio
async def test_hold_is_bounded_and_then_surfaces_502(monkeypatch):
    """A verdict that never lands still gets an honest answer, not a hang."""
    clock = {"t": 0.0}
    monkeypatch.setattr(routes.time, "monotonic", lambda: clock["t"])
    upstream = FlakyUpstream(fail_times=99)
    # `deciding` stays True: the failure clock never advances past the gate.
    br = _breaker(min_outage=20.0, now_fn=lambda: 0.0)
    _wire(monkeypatch, upstream, br)

    real_sleep = routes.asyncio.sleep

    async def tick(_):
        clock["t"] += 1.0
        await real_sleep(0)

    monkeypatch.setattr(routes.asyncio, "sleep", tick)

    with pytest.raises(routes.HTTPException) as exc:
        await routes._try_upstream(_Req(), b"{}", Settings(router_mode="hybrid"))

    assert exc.value.status_code == 502
    cap = br.min_outage + routes._HOLD_GRACE_SECONDS
    assert clock["t"] >= cap, "must hold the full window before giving up"
    assert clock["t"] < cap + 5, "must not hold appreciably past the window"


@pytest.mark.asyncio
async def test_delivered_verdict_still_502s_immediately(monkeypatch):
    """`online, so relay the error` is an answer — do not sit on it.

    The breaker resets `_failures` to 0 in that branch, so `deciding` is False
    and the turn must surface at once instead of buying backoff-free latency
    for an upstream the breaker has explicitly declined to fail over for.
    """
    clock = {"t": 0.0}
    monkeypatch.setattr(routes.time, "monotonic", lambda: clock["t"])
    upstream = FlakyUpstream(fail_times=99)
    # require_offline + an online host is exactly the "relay the error" verdict.
    br = _breaker(min_outage=0.0, require_offline=True, online_fn=lambda: True)
    _wire(monkeypatch, upstream, br)

    slept = []
    monkeypatch.setattr(routes.asyncio, "sleep", lambda d: slept.append(d))

    with pytest.raises(routes.HTTPException) as exc:
        await routes._try_upstream(_Req(), b"{}", Settings(router_mode="hybrid"))

    assert exc.value.status_code == 502
    assert slept == [], "a delivered verdict must not be held"
    assert not br.deciding


# --- QA #132: the hold must bound its own cost, and never re-send blindly ----


class SlowUpstream:
    """Each send hangs for `send_seconds` on the injected clock, then fails."""

    def __init__(self, send_seconds, clock, exc=None):
        self.send_seconds = send_seconds
        self.clock = clock
        self.exc = exc or httpx.ConnectTimeout("timed out")
        self.sends = 0

    def build_request(self, method, url, *, content, headers):
        return httpx.Request(method, f"https://api.anthropic.com{url}",
                             content=content, headers=headers)

    async def send(self, request, *, stream):
        self.sends += 1
        self.clock["t"] += self.send_seconds
        raise self.exc

    async def aclose(self):
        pass


class DeliveredFailureUpstream:
    """Fails with an error that can occur AFTER the request was accepted.

    Advances the clock a little per send so the hold reaches its deadline and
    the test terminates. Without that the pre-fix loop re-sends forever on a
    frozen clock, which hangs the suite instead of failing it.
    """

    def __init__(self, exc, clock, send_seconds=1.0):
        self.exc = exc
        self.clock = clock
        self.send_seconds = send_seconds
        self.sends = 0

    def build_request(self, method, url, *, content, headers):
        return httpx.Request(method, f"https://api.anthropic.com{url}",
                             content=content, headers=headers)

    async def send(self, request, *, stream):
        self.sends += 1
        self.clock["t"] += self.send_seconds
        raise self.exc

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_hold_does_not_resend_a_request_that_may_have_been_delivered(monkeypatch):
    """A POST to /v1/messages is not idempotent, so an ambiguous failure ends re-sending.

    A read timeout or a dropped connection can happen after Anthropic accepted
    the request and began generating. Re-sending then bills a second generation
    the user never asked for, and the hold loop could do it several times a
    second, invisibly. Only failures that prove the request never left may be
    repeated; the rest hold for the verdict without re-sending.
    """
    clock = {"t": 0.0}
    monkeypatch.setattr(routes.time, "monotonic", lambda: clock["t"])

    # Wall-clock advances during a hold even when nothing is being sent. The
    # fake clock only moves inside send(), so model the sleep too — otherwise a
    # hold that correctly stops re-sending never reaches its deadline here.
    async def _sleep(seconds):
        clock["t"] += max(seconds, 1.0)

    monkeypatch.setattr(routes.asyncio, "sleep", _sleep)

    for exc in (httpx.ReadTimeout("read timed out"),
                httpx.ReadError("connection lost"),
                httpx.RemoteProtocolError("server disconnected"),
                httpx.WriteError("broken pipe")):
        upstream = DeliveredFailureUpstream(exc, clock)
        br = _breaker(now_fn=lambda: clock["t"])
        _wire(monkeypatch, upstream, br)
        clock["t"] = 0.0

        with pytest.raises(routes.HTTPException) as caught:
            await routes._try_upstream(_Req(), b"{}", Settings(router_mode="hybrid"))

        assert caught.value.status_code == 502
        assert upstream.sends == 1, (
            f"{type(exc).__name__} may have been delivered; it must not be re-sent "
            f"(was sent {upstream.sends}x)"
        )


@pytest.mark.asyncio
async def test_hold_still_resends_a_failure_that_proves_nothing_was_sent(monkeypatch):
    """A connect-phase failure never reached Anthropic, so retrying is free."""
    clock = {"t": 0.0}
    monkeypatch.setattr(routes.time, "monotonic", lambda: clock["t"])
    upstream = FlakyUpstream(fail_times=2, exc=httpx.ConnectError("refused"))
    br = _breaker(now_fn=lambda: clock["t"])
    _wire(monkeypatch, upstream, br)

    resp = await routes._try_upstream(_Req(), b"{}", Settings(router_mode="hybrid"))
    assert resp is not None
    assert upstream.sends == 3


@pytest.mark.asyncio
async def test_hold_bounds_the_time_spent_inside_a_send(monkeypatch):
    """The deadline must cap the hold itself, not just the gaps between sends.

    `hold_deadline` was only consulted between attempts, so a send starting one
    tick before it could run its full client timeout past it: the turn was held
    for the first attempt, plus the gate, plus another entire attempt. That is
    the long freeze this hold exists to prevent, rebuilt inside the fix.

    Real time here, deliberately. The bound is enforced by `asyncio.wait_for`,
    which measures the event loop's own clock, so a frozen fake clock cannot
    exercise it — the earlier version of this test passed a broken build. The
    numbers are small so the test stays fast.
    """
    monkeypatch.setattr(routes, "_HOLD_GRACE_SECONDS", 0.1)

    class FastThenHanging:
        """Fails instantly once (safe to retry), then hangs well past the budget."""

        def __init__(self):
            self.sends = 0

        def build_request(self, method, url, *, content, headers):
            return httpx.Request(method, f"https://api.anthropic.com{url}",
                                 content=content, headers=headers)

        async def send(self, request, *, stream):
            # `_upstream_send` retries a pre-send failure once internally, so
            # the first _try_upstream attempt spends two sends here. Both fail
            # fast; the hang belongs to the NEXT attempt, which is the one the
            # deadline has to bound.
            self.sends += 1
            if self.sends <= 2:
                raise httpx.ConnectError("refused")
            await asyncio.sleep(30)
            raise httpx.ConnectError("refused")

        async def aclose(self):
            pass

    upstream = FastThenHanging()
    br = _breaker(min_outage=0.2)
    _wire(monkeypatch, upstream, br)

    started = time.monotonic()
    try:
        await routes._try_upstream(_Req(), b"{}", Settings(router_mode="hybrid"))
    except routes.HTTPException:
        pass
    elapsed = time.monotonic() - started

    assert upstream.sends >= 3, "the hold should have re-attempted the safe failure"
    # Budget is min_outage + grace = 0.3s. Anything approaching the 30s hang
    # means the deadline did not bound the send.
    assert elapsed < 5.0, f"held {elapsed:.1f}s; the 30s send was not bounded"
