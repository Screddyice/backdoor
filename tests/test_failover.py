"""Unit tests for the cloud→local failover breaker."""

from src.proxy.failover import FailoverBreaker


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(clock, **kw):
    notes = []
    br = FailoverBreaker(
        threshold=kw.pop("threshold", 3),
        window=kw.pop("window", 120.0),
        probe_interval=kw.pop("probe_interval", 60.0),
        now_fn=clock,
        notify_fn=lambda title, msg: notes.append(msg),
    )
    return br, notes


def test_opens_after_threshold_consecutive_failures():
    clock = Clock()
    br, notes = make(clock)
    assert br.record_failure("ConnectError") is False
    assert br.record_failure("ConnectError") is False
    assert br.record_failure("ConnectError") is True   # trips on the 3rd
    assert br.open
    assert len(notes) == 1


def test_success_resets_consecutive_count():
    clock = Clock()
    br, _ = make(clock)
    br.record_failure("HTTP 429")
    br.record_failure("HTTP 429")
    br.record_success()                                 # normal backpressure recovered
    assert br.record_failure("HTTP 429") is False       # count restarted at 1
    assert not br.open


def test_stale_failures_outside_window_do_not_accumulate():
    clock = Clock()
    br, _ = make(clock, window=120.0)
    br.record_failure("ConnectError")
    br.record_failure("ConnectError")
    clock.t += 200                                      # past the window
    assert br.record_failure("ConnectError") is False   # counts as a fresh run of 1
    assert not br.open


def test_open_gates_upstream_to_one_probe_per_interval():
    clock = Clock()
    br, _ = make(clock, probe_interval=60.0)
    for _ in range(3):
        br.record_failure("ConnectError")
    assert br.open
    assert br.allow_upstream() is False                 # just opened, probe timer armed
    clock.t += 61
    assert br.allow_upstream() is True                  # one probe allowed
    assert br.allow_upstream() is False                 # and only one
    clock.t += 61
    assert br.allow_upstream() is True


def test_probe_success_closes_and_notifies():
    clock = Clock()
    br, notes = make(clock)
    for _ in range(3):
        br.record_failure("HTTP 529")
    assert br.open
    br.record_success()
    assert not br.open
    assert br.allow_upstream() is True
    assert len(notes) == 2                              # opened + recovered


def test_failure_while_open_keeps_serving_locally():
    clock = Clock()
    br, _ = make(clock)
    for _ in range(3):
        br.record_failure("ConnectError")
    clock.t += 61
    assert br.allow_upstream() is True                  # probe
    assert br.record_failure("ConnectError") is True    # probe failed → stay local
    assert br.open


def test_closed_always_allows_upstream():
    clock = Clock()
    br, _ = make(clock)
    for _ in range(10):
        assert br.allow_upstream() is True


# ── Failover ladder (size → local tier) ──────────────────────────────────────
from src.proxy.config import pick_failover_profile, FAILOVER_LADDER


def test_ladder_small_session_stays_on_4b():
    assert pick_failover_profile(0) == "local-qwen35"
    assert pick_failover_profile(52_000) == "local-qwen35"


def test_ladder_medium_session_escalates_to_9b_128k():
    assert pick_failover_profile(52_001) == "local-failover-128k"
    assert pick_failover_profile(115_000) == "local-failover-128k"


def test_ladder_large_session_escalates_to_9b_256k():
    assert pick_failover_profile(115_001) == "local-failover-256k"
    assert pick_failover_profile(10_000_000) == "local-failover-256k"


def test_ladder_bounds_are_monotonic():
    bounds = [b for b, _ in FAILOVER_LADDER]
    assert bounds == sorted(bounds)
    assert bounds[-1] == float("inf")
