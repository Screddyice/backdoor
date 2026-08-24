"""Unit tests for the cloud→local failover breaker.

The breaker opens on exactly one condition: this host is offline. Tests inject
connectivity through `online_fn` rather than touching the network, and redirect
the published state file so a test run never writes to ~/.backdoor.
"""

import contextlib
import json
import ssl
import tempfile
from pathlib import Path

from src.proxy.failover import (
    FAILOVER_STATUSES,
    FailoverBreaker,
    _statuses_from_env,
    internet_reachable,
)

_STATE_DIR = Path(tempfile.mkdtemp(prefix="backdoor-failover-state-"))
_state_seq = iter(range(1_000_000))


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(clock, **kw):
    notes = []
    state_path = _STATE_DIR / f"state-{next(_state_seq)}.json"
    br = FailoverBreaker(
        threshold=kw.pop("threshold", 3),
        window=kw.pop("window", 120.0),
        probe_interval=kw.pop("probe_interval", 60.0),
        now_fn=clock,
        notify_fn=lambda title, msg: notes.append(msg),
        # Offline by default: these exercise breaker mechanics, and the breaker
        # only has mechanics to exercise when the host has no internet.
        online_fn=kw.pop("online_fn", lambda: False),
        state_path=kw.pop("state_path", state_path),
    )
    br.state_path = state_path
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
    br.record_failure("ConnectError")
    br.record_failure("ConnectError")
    br.record_success()                                 # upstream recovered
    assert br.record_failure("ConnectError") is False   # count restarted at 1
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
        br.record_failure("ConnectError")
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


# ── Only genuine internet loss may open the breaker ──────────────────────────
# Opening the breaker claims the local GPU (a qwen tier, up to ~13 GB) in the
# same Ollama server the llm-jury council needs. That is only justified when a
# session has no other way to survive, i.e. when this host is actually offline.


def test_does_not_open_while_this_host_is_online():
    """Anthropic unreachable but the internet fine: relay, do not claim the GPU."""
    clock = Clock()
    br, notes = make(clock, online_fn=lambda: True)
    for _ in range(10):
        assert br.record_failure("ConnectError") is False
    assert not br.open
    assert notes == []                                  # no "routing to local" alert


def test_online_refusal_resets_the_count_so_probes_are_not_per_request():
    """Reaching the threshold while online costs one probe, not one per request."""
    probes = []
    clock = Clock()
    br, _ = make(clock, online_fn=lambda: probes.append(1) or True)
    for _ in range(9):
        br.record_failure("ConnectError")
    assert not br.open
    assert len(probes) == 3                             # once per run of `threshold`


def test_offline_after_being_online_still_opens():
    """The connectivity verdict is re-taken, not cached from an earlier check."""
    clock = Clock()
    online = {"v": True}
    br, _ = make(clock, online_fn=lambda: online["v"])
    for _ in range(3):
        br.record_failure("ConnectError")
    assert not br.open
    online["v"] = False
    for _ in range(3):
        br.record_failure("ConnectError")
    assert br.open


def test_http_statuses_are_not_triggers_by_default():
    """A 429/529 is proof the network works — it must never arm failover."""
    assert FAILOVER_STATUSES == set()


def test_default_threshold_is_one():
    """A latency contract, so raising it back is a deliberate act.

    Measured 2026-08-09: Claude Code retries a dead upstream persistently (9+
    attempts over 107s), so the count is never what stops the breaker opening —
    it only decides how long the user waits first. Three failures cost ~15-20s
    before the connectivity probe even runs, on top of ~10s of model load.

    Safety does not come from the count. `internet_reachable` gets the final
    say, so a transient blip still cannot open the breaker at any threshold.
    """
    from src.proxy.config import Settings
    assert Settings().failover_threshold == 1
    assert FailoverBreaker().threshold == 1


def test_statuses_can_be_restored_via_env(monkeypatch):
    monkeypatch.setenv("BACKDOOR_FAILOVER_STATUSES", "429, 529 ,junk")
    assert _statuses_from_env() == {429, 529}
    monkeypatch.setenv("BACKDOOR_FAILOVER_STATUSES", "")
    assert _statuses_from_env() == set()


# ── The connectivity probe ───────────────────────────────────────────────────
#
# These drive `socket.create_connection` directly, per this module's contract of
# injecting connectivity rather than touching the network.
#
# The offline case used to assert against TEST-NET-1 (192.0.2.1), which RFC 5737
# reserves and promises is unroutable. That promise binds the public internet,
# not the machine running the suite, and it is exactly the wrong thing to lean
# on here: a network that answers TCP on every address is the failure mode this
# probe exists to detect, so borrowing the routing table as a fixture meant the
# test broke on precisely the networks the code most needs to be right about.
# Verified 2026-08-17 — this host completes a connection to 192.0.2.1:443 in
# ~0.2s, so the test failed while `internet_reachable` was behaving correctly.

_PROBES = (("1.1.1.1", 443, "one.one.one.one"), ("8.8.8.8", 443, "dns.google"))


def _refuse(address, timeout=None):
    raise OSError(101, "Network is unreachable")


class _FakeSocket:
    """Stands in for a connected socket. Records the handshake timeout it got."""

    def __init__(self, record=None):
        self.record = record

    def settimeout(self, value):
        if self.record is not None:
            self.record.append(value)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _accepting_tls(monkeypatch, record=None):
    """Every probe connects AND presents a certificate that verifies."""
    monkeypatch.setattr(
        "src.proxy.failover.socket.create_connection",
        lambda address, timeout=None: _FakeSocket(record),
    )
    monkeypatch.setattr(
        "src.proxy.failover._tls_context",
        lambda: type("Ctx", (), {"wrap_socket": lambda self, sock, server_hostname=None: contextlib.nullcontext()})(),
    )


def test_internet_probe_reports_offline_when_no_probe_connects(monkeypatch):
    """Offline means every probe refused, not that one address looked dead."""
    tried = []

    def refuse(address, timeout=None):
        tried.append(address)
        return _refuse(address, timeout)

    monkeypatch.setattr("src.proxy.failover.socket.create_connection", refuse)

    assert internet_reachable(probes=_PROBES) is False
    assert tried == [("1.1.1.1", 443), ("8.8.8.8", 443)], (
        "every probe must be tried before declaring offline"
    )


def test_internet_probe_reports_online_on_the_first_connection(monkeypatch):
    """One verified address is enough, and it stops dialling there."""
    tried = []
    _accepting_tls(monkeypatch)

    def accept(address, timeout=None):
        tried.append(address)
        return _FakeSocket()

    monkeypatch.setattr("src.proxy.failover.socket.create_connection", accept)

    assert internet_reachable(probes=_PROBES) is True
    assert tried == [("1.1.1.1", 443)], "a verified probe must short-circuit the rest"


def test_internet_probe_survives_one_dead_probe(monkeypatch):
    """A single blackholed resolver is not a dead network.

    This is the case that decides whether the breaker wrongly claims the GPU. If
    one refusal short-circuited to offline, a host with 1.1.1.1 filtered — common
    on corporate DNS — would load 17 GB and route the session to a local model
    while the cloud was reachable the whole time.
    """
    _accepting_tls(monkeypatch)

    def first_probe_fails(address, timeout=None):
        if address == ("1.1.1.1", 443):
            return _refuse(address, timeout)
        return _FakeSocket()

    monkeypatch.setattr("src.proxy.failover.socket.create_connection", first_probe_fails)

    assert internet_reachable(probes=_PROBES) is True


def test_internet_probe_honours_the_timeout_it_is_given(monkeypatch):
    """The timeout is a latency contract: it bounds how long a turn can stall."""
    seen = []

    def record(address, timeout=None):
        seen.append(timeout)
        return _refuse(address, timeout)

    monkeypatch.setattr("src.proxy.failover.socket.create_connection", record)

    internet_reachable(probes=_PROBES, timeout=0.25)
    assert seen == [0.25, 0.25]


def test_one_unverifiable_probe_does_not_end_the_check(monkeypatch):
    """Interception is per-route, so a failed handshake must not stop the loop.

    The mirror of `survives_one_dead_probe`, for the verification path. A host
    whose route to 1.1.1.1 is hijacked while 8.8.8.8 is clean is online, and
    giving up at the first unverifiable peer would report it offline and claim
    the GPU. Cheap to get wrong: the natural way to write the handler is to
    return, not continue.
    """
    monkeypatch.setattr(
        "src.proxy.failover.socket.create_connection",
        lambda address, timeout=None: _FakeSocket(),
    )

    class Ctx:
        def wrap_socket(self, sock, server_hostname=None):
            if server_hostname == "one.one.one.one":
                raise ssl.SSLCertVerificationError("hostname mismatch")
            return contextlib.nullcontext()

    monkeypatch.setattr("src.proxy.failover._tls_context", Ctx)

    assert internet_reachable(probes=_PROBES) is True


def test_timeout_also_bounds_the_tls_handshake(monkeypatch):
    """The handshake, not the connect, is what hangs against a silent acceptor."""
    handshake_timeouts = []
    _accepting_tls(monkeypatch, record=handshake_timeouts)

    assert internet_reachable(probes=_PROBES, timeout=0.25) is True
    assert handshake_timeouts == [0.25]


# ── The middlebox hole ───────────────────────────────────────────────────────
#
# These two use a real loopback listener rather than a mock. That is not a
# relapse into testing the network: loopback needs no network, behaves the same
# on every host, and is the only way to exercise the actual failure — a peer
# that completes a TCP handshake and then cannot prove who it is.


def _accept_only_listener():
    """A transparent middlebox in miniature: accepts TCP, speaks nothing."""
    import socket as _socket
    import threading

    srv = _socket.socket()
    srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)

    # Hold every accepted socket. Dropping it would let refcounting close the
    # connection, and the client would fail fast on EOF instead of hanging —
    # which is not what a middlebox does, and would test the wrong thing.
    held = []

    def accept_forever():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            held.append(conn)

    threading.Thread(target=accept_forever, daemon=True).start()
    return srv, srv.getsockname()[1]


def test_a_box_that_answers_every_address_reads_as_offline():
    """The regression test for the reason this probe was rewritten.

    Captive portals, some corporate and hotel networks, and certain VPN
    configurations complete a TCP connection to any address, including ones RFC
    5737 guarantees are unroutable — measured at ~0.2s on the network this repo
    is developed on. The old probe read that as "online", so with the internet
    gone the breaker never opened and failover silently did not happen, which is
    the single situation the whole mechanism exists for.

    Verifying the certificate closes it: the box has nothing the trust store
    accepts for `one.one.one.one`.
    """
    srv, port = _accept_only_listener()
    try:
        assert internet_reachable(
            probes=(("127.0.0.1", port, "one.one.one.one"),), timeout=0.25
        ) is False
    finally:
        srv.close()


def test_tcp_only_escape_hatch_restores_the_old_behaviour(monkeypatch):
    """A false offline claims the GPU, so the strict probe must be switchable off.

    Same listener, same probe, opposite verdict — which also proves the listener
    genuinely accepts the connection, so the test above fails on verification
    rather than on connectivity.
    """
    monkeypatch.setenv("BACKDOOR_PROBE_TCP_ONLY", "1")
    srv, port = _accept_only_listener()
    try:
        assert internet_reachable(
            probes=(("127.0.0.1", port, "one.one.one.one"),), timeout=0.25
        ) is True
    finally:
        srv.close()


# ── Published state: the handshake llm-jury reads ────────────────────────────


def _state(br):
    return json.loads(br.state_path.read_text(encoding="utf-8"))


def test_state_file_starts_inactive():
    clock = Clock()
    br, _ = make(clock)
    assert _state(br)["failover_active"] is False


def test_state_file_publishes_open_then_close():
    clock = Clock()
    br, _ = make(clock)
    for _ in range(3):
        br.record_failure("ConnectError")
    published = _state(br)
    assert published["failover_active"] is True
    assert published["reason"] == "ConnectError"
    br.record_success()
    assert _state(br)["failover_active"] is False


def test_state_file_stays_inactive_when_online():
    """The file tracks GPU ownership, so a non-opening failure must not set it."""
    clock = Clock()
    br, _ = make(clock, online_fn=lambda: True)
    for _ in range(5):
        br.record_failure("ConnectError")
    assert _state(br)["failover_active"] is False


def test_unwritable_state_path_does_not_break_the_breaker():
    """Publishing is best-effort: a router that cannot write must still route."""
    clock = Clock()
    br, _ = make(clock, state_path=Path("/proc/nonexistent/state.json"))
    for _ in range(3):
        br.record_failure("ConnectError")
    assert br.open


# ── Failover ladder (size → local tier) ──────────────────────────────────────
from src.proxy.config import pick_failover_profile, FAILOVER_LADDER


def test_ladder_normal_session_gets_the_strong_tool_capable_tier():
    """The common case after bare-mode stripping: a small prompt, so the
    strongest local model rather than the widest-window one."""
    assert pick_failover_profile(0) == "local-failover-qwen27"
    assert pick_failover_profile(28_000) == "local-failover-qwen27"


def test_ladder_oversize_session_falls_back_to_the_wide_4b():
    """Bare mode bounds the harness but not the conversation. A transcript that
    still overflows the 27B's 32K window must keep its context on the 256K 4B —
    a weaker model that remembers the session beats a stronger one that
    truncates it."""
    assert pick_failover_profile(28_001) == "local-failover-256k"
    assert pick_failover_profile(10_000_000) == "local-failover-256k"


def test_ladder_tiers_have_profile_files():
    """A ladder entry naming a profile that does not exist fails only at the
    moment of an outage, which is the worst possible time to discover it."""
    import os
    for _, profile in FAILOVER_LADDER:
        assert os.path.exists(f"profiles/{profile}.env"), profile


def test_ladder_bounds_are_monotonic():
    bounds = [b for b, _ in FAILOVER_LADDER]
    assert bounds == sorted(bounds)
    assert bounds[-1] == float("inf")
