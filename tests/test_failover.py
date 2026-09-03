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

import pytest

from src.proxy.failover import (
    FAILOVER_STATUSES,
    FailoverBreaker,
    _statuses_from_env,
    internet_reachable,
    service_reachable,
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
        **kw,
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


def test_independent_breakers_publish_aggregate_gpu_ownership(tmp_path):
    """Closing Codex must not hide an active Anthropic GPU claim, or vice versa."""
    clock = Clock()
    state_path = tmp_path / "shared-state.json"
    anthropic = FailoverBreaker(
        threshold=1,
        now_fn=clock,
        notify_fn=lambda *_: None,
        online_fn=lambda: False,
        state_path=state_path,
        source="anthropic",
        upstream_name="Anthropic",
    )
    codex = FailoverBreaker(
        threshold=1,
        now_fn=clock,
        notify_fn=lambda *_: None,
        online_fn=lambda: False,
        state_path=state_path,
        source="codex",
        upstream_name="ChatGPT Codex",
    )

    assert anthropic.record_failure("AnthropicConnectError") is True
    assert codex.record_failure("CodexConnectError") is True
    anthropic.record_success()

    published = json.loads(state_path.read_text(encoding="utf-8"))
    assert published["failover_active"] is True
    assert published["active_sources"] == ["codex"]
    assert published["reasons"] == {"codex": "CodexConnectError"}

    codex.record_success()
    published = json.loads(state_path.read_text(encoding="utf-8"))
    assert published["failover_active"] is False
    assert published["active_sources"] == []
    assert published["reasons"] == {}


def test_codex_breaker_can_open_for_service_failure_while_host_is_online(tmp_path):
    """The Codex policy can cover usage limits without weakening Anthropic."""
    br = FailoverBreaker(
        threshold=1,
        notify_fn=lambda *_: None,
        online_fn=lambda: True,
        state_path=tmp_path / "codex-state.json",
        source="codex",
        upstream_name="ChatGPT Codex",
        require_offline=False,
    )

    assert br.record_failure("HTTP 429") is True
    assert br.open is True


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
    assert pick_failover_profile(0) == "local-qwen38-obliterated"
    assert pick_failover_profile(27_000) == "local-qwen38-obliterated"
    assert pick_failover_profile(27_001) == "local-failover-256k"


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


# ── The handshake-timeout hole ───────────────────────────────────────────────
#
# The mirror of the middlebox hole above, and the reason internet_reachable grew
# a second attempt on 2026-09-03. Reading every non-verifying outcome as the
# middlebox lie made a slow link indistinguishable from a box answering for the
# whole internet, and the router opened the breaker three times on the evening of
# 2026-09-02 against a working connection — every one logging `The handshake
# operation timed out`, not one logging a certificate error.


class _TimingOutThenVerifying:
    """A peer too slow for the first budget and fast enough for the patient one."""

    def __init__(self, patient_at: float):
        self.patient_at = patient_at
        self.timeouts: list[float] = []

    def settimeout(self, value):
        self.timeouts.append(value)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_slow_handshake_gets_a_patient_second_attempt(monkeypatch):
    """A link that stalls is online, and must not be read as a lying middlebox.

    This is the false-open case. Opening the breaker is expensive in exactly the
    way this module warns about — it claims the GPU, evicts the llm-jury council,
    and silently downgrades live sessions to a local model — so an outcome that
    only means "not yet" must not be spent as if it meant "offline".
    """
    budgets = []

    def connect(address, timeout=None):
        return _TimingOutThenVerifying(1.0)

    class Ctx:
        def wrap_socket(self, sock, server_hostname=None):
            budget = sock.timeouts[-1]
            budgets.append(budget)
            if budget < 1.0:
                raise TimeoutError("_ssl.c:1064: The handshake operation timed out")
            return contextlib.nullcontext()

    monkeypatch.setattr("src.proxy.failover.socket.create_connection", connect)
    monkeypatch.setattr("src.proxy.failover._tls_context", Ctx)

    assert internet_reachable(probes=_PROBES, timeout=0.25) is True
    assert budgets == [0.25, 1.0], (
        "the timeout must be retried once at CONNECTIVITY_SLOW_FACTOR x the budget"
    )


def test_a_certificate_error_is_not_worth_retrying(monkeypatch):
    """An impostor is a definite answer — more time cannot change it.

    Retrying it would double the stall on every genuine captive portal for no
    new information, so the patient attempt must be reserved for the ambiguous
    outcome.
    """
    attempts = []

    def connect(address, timeout=None):
        attempts.append((address, timeout))
        return _FakeSocket()

    class Ctx:
        def wrap_socket(self, sock, server_hostname=None):
            raise ssl.SSLCertVerificationError("hostname mismatch")

    monkeypatch.setattr("src.proxy.failover.socket.create_connection", connect)
    monkeypatch.setattr("src.proxy.failover._tls_context", Ctx)

    assert internet_reachable(probes=_PROBES, timeout=0.25) is False
    assert [t for _, t in attempts] == [0.25, 0.25], (
        "each probe gets one attempt when the peer proves it is someone else"
    )


def test_a_persistently_silent_box_still_reads_as_offline():
    """The patient retry must not re-open the hole the verification closed.

    A real silent acceptor never completes a handshake at any budget, so giving
    it more room changes nothing — which is the whole reason more room is a safe
    way to tell it apart from a slow peer.
    """
    srv, port = _accept_only_listener()
    try:
        assert internet_reachable(
            probes=(("127.0.0.1", port, "one.one.one.one"),), timeout=0.05
        ) is False
    finally:
        srv.close()


# ── Recovery without a rider ─────────────────────────────────────────────────
#
# Until 2026-09-03 an OPEN breaker could only close when a real /v1/messages
# passthrough happened to arrive and succeed. An outage removes those requests —
# a session on a local 4B generates far less traffic, and an abandoned one none —
# so recovery depended on the thing the outage takes away. Measured 2026-09-02:
# open 22:28:14, closed 23:45:51, 77m37s, closing the second outside traffic hit.


def test_recovery_probe_closes_the_breaker_with_no_traffic():
    clock = Clock()
    online = [False]
    br, notes = make(clock, threshold=1, online_fn=lambda: online[0])

    assert br.record_failure("ConnectError") is True
    assert br.open

    online[0] = True                       # the network came back
    clock.t += 60.0
    assert br.maybe_recover() is True       # no request needed
    assert not br.open
    assert br.reason == ""


def test_recovery_probe_respects_the_probe_interval():
    clock = Clock()
    calls = []

    def online():
        calls.append(clock.t)
        return True

    br, _ = make(clock, threshold=1, online_fn=lambda: False)
    br.record_failure("ConnectError")
    assert br.open

    br._online = online
    clock.t += 59.0
    assert br.maybe_recover() is False
    assert calls == [], "the probe must not run before the interval elapses"

    clock.t += 1.0
    assert br.maybe_recover() is True


def test_recovery_probe_leaves_a_still_offline_host_open():
    clock = Clock()
    br, _ = make(clock, threshold=1)         # online_fn stays False
    br.record_failure("ConnectError")
    assert br.open

    clock.t += 600.0
    assert br.maybe_recover() is False
    assert br.open, "still offline: the reason the breaker opened has not gone away"


def test_recovery_probe_is_a_no_op_while_closed():
    clock = Clock()
    br, notes = make(clock, threshold=1, online_fn=lambda: True)
    assert br.maybe_recover() is False
    assert not br.open
    assert notes == [], "a closed breaker must not announce a recovery"


def test_recovery_probe_does_not_starve_the_half_open_slot():
    """The two timers are independent on purpose.

    Sharing one would let the recovery probe consume the slot that lets a real
    request try upstream, trading one starvation for another.
    """
    clock = Clock()
    br, _ = make(clock, threshold=1, online_fn=lambda: False)
    br.record_failure("ConnectError")
    assert br.open

    clock.t += 60.0
    assert br.maybe_recover() is False       # spends the recovery timer
    assert br.allow_upstream() is True, "half-open must still get its own turn"


def test_recovery_probe_waits_a_full_interval_after_opening():
    """Opening already ran the probe; re-running it immediately proves nothing."""
    clock = Clock()
    calls = []

    def online():
        calls.append(clock.t)
        return False

    br, _ = make(clock, threshold=1, online_fn=online)
    br.record_failure("ConnectError")
    assert br.open
    assert len(calls) == 1                   # the open's own probe

    assert br.maybe_recover() is False
    assert len(calls) == 1, "no second probe in the same instant the breaker opened"


def test_recovery_probe_refuses_a_service_level_breaker_with_no_probe():
    """A breaker that never asked about the network cannot be answered by it.

    The Codex upstream runs `require_offline=False`: it opens on consecutive
    service failures alone, so it can be open while this host's connectivity is
    perfect. Closing it on an online probe would reopen it on the very next
    request, every interval, forever — and each cycle would throw a real request
    at a service already known to be down. Its only honest route back is the
    half-open path, which actually reaches the service.
    """
    clock = Clock()
    br, _ = make(
        clock, threshold=1, require_offline=False, online_fn=lambda: True
    )
    assert br.record_failure("HTTP 429", transport_error=False) is True
    assert br.open, "a service-level breaker opens without consulting the probe"

    clock.t += 600.0
    assert br.maybe_recover() is False
    assert br.open, "connectivity says nothing about whether the service is back"


def test_recovery_probe_still_serves_an_offline_gated_breaker():
    """The mirror: require_offline=True is exactly where the probe is decisive."""
    clock = Clock()
    online = [False]
    br, _ = make(
        clock, threshold=1, require_offline=True, online_fn=lambda: online[0]
    )
    br.record_failure("ConnectError")
    assert br.open

    online[0] = True
    clock.t += 60.0
    assert br.maybe_recover() is True
    assert not br.open


# ── Probing the service, not the internet ────────────────────────────────────
#
# A service-level breaker (the Codex upstream) opens without consulting
# connectivity, so `internet_reachable` cannot answer it. What this router *can*
# ask on its own is whether that upstream's host is reachable — it relays the
# caller's credentials and holds none, so it cannot make an authenticated Codex
# request outside a real one. That measurement disproves exactly one kind of
# open, and the breaker must not pretend otherwise.


def test_service_probe_closes_a_breaker_that_opened_on_a_transport_error():
    clock = Clock()
    reachable = [False]
    br, _ = make(
        clock,
        threshold=1,
        require_offline=False,
        service_fn=lambda: reachable[0],
    )
    assert br.record_failure("ConnectTimeout") is True
    assert br.open

    reachable[0] = True
    clock.t += 60.0
    assert br.maybe_recover() is True
    assert not br.open


def test_service_probe_will_not_close_a_breaker_that_opened_on_a_status():
    """Reachability disproves "I could not reach it", never "it answered 429".

    The front door of a rate-limited service is perfectly reachable. Closing on
    that would reopen on the next request, every interval, and spend real
    requests against a service that already said no — while the router cannot
    even ask properly, since the credential belongs to the caller.
    """
    clock = Clock()
    br, _ = make(
        clock,
        threshold=1,
        require_offline=False,
        service_fn=lambda: True,          # host is up; quota is not
    )
    assert br.record_failure("HTTP 429", transport_error=False) is True
    assert br.open

    clock.t += 600.0
    assert br.maybe_recover() is False
    assert br.open


def test_service_probe_leaves_an_unreachable_upstream_open():
    clock = Clock()
    br, _ = make(
        clock, threshold=1, require_offline=False, service_fn=lambda: False
    )
    br.record_failure("ConnectError")
    assert br.open
    clock.t += 60.0
    assert br.maybe_recover() is False
    assert br.open


def test_an_offline_gated_breaker_still_uses_the_connectivity_probe():
    """The Claude breaker must not start asking the service instead."""
    clock = Clock()
    asked = []
    online = [False]                       # offline, so the breaker may open

    def online_fn():
        asked.append("online")
        return online[0]

    br, _ = make(
        clock,
        threshold=1,
        require_offline=True,
        online_fn=online_fn,
        service_fn=lambda: (asked.append("service"), True)[1],
    )
    br.record_failure("ConnectError")
    assert br.open

    online[0] = True
    clock.t += 60.0
    assert br.maybe_recover() is True
    assert "service" not in asked, "an offline-gated breaker asks connectivity"


def test_service_reachable_targets_the_url_s_host_and_port(monkeypatch):
    seen = []
    monkeypatch.setattr(
        "src.proxy.failover._probe_all",
        lambda probes, timeout, verify: seen.append((tuple(probes), verify)) or True,
    )
    assert service_reachable("https://chatgpt.com/backend-api/codex") is True
    assert seen[0][0] == (("chatgpt.com", 443, "chatgpt.com"),), (
        "the certificate must be checked against the service's own name"
    )
    assert seen[0][1] is True


def test_service_reachable_skips_verification_for_plain_http(monkeypatch):
    """There is no certificate to check on http://, so a handshake is the wrong test."""
    seen = []
    monkeypatch.setattr(
        "src.proxy.failover._probe_all",
        lambda probes, timeout, verify: seen.append((tuple(probes), verify)) or True,
    )
    assert service_reachable("http://127.0.0.1:11434/v1") is True
    assert seen[0][0] == (("127.0.0.1", 11434, "127.0.0.1"),)
    assert seen[0][1] is False


def test_service_reachable_reports_false_for_an_unparseable_url(monkeypatch):
    monkeypatch.setattr(
        "src.proxy.failover._probe_all",
        lambda *a, **k: pytest.fail("must not dial when there is no host"),
    )
    assert service_reachable("") is False
