"""Cloud→local failover breaker for hybrid mode.

When this machine cannot reach the internet, hybrid-mode passthrough requests
are served by a local Ollama profile instead of failing, so an in-flight Claude
Code session keeps going.

Shape: a classic circuit breaker.

  CLOSED  every passthrough request goes upstream. A transport error increments
          a consecutive-failure counter; any response resets it. Errors below
          the threshold are relayed verbatim so the client's own retry logic
          still runs.
  OPEN    reached after `threshold` consecutive failures inside `window`
          seconds AND a connectivity probe confirming this host is offline.
          Passthrough-bound /v1/messages requests are served by the failover
          profile. One request per `probe_interval` is allowed to try upstream
          (half-open); a success closes the breaker. Independently of any
          traffic, :meth:`FailoverBreaker.maybe_recover` re-runs the connectivity
          probe on the same interval and closes the breaker once this host is
          online again — an outage removes the very requests half-open needs.

**Opening the breaker means exactly one thing: this machine is offline.** That
narrowness is deliberate and load-bearing, because failing over is not free —
it loads a qwen tier (up to ~13 GB) into the same Ollama server the llm-jury
council needs, on a host where the council already wants ~23 GB of a 36 GB
budget. Two local-GPU consumers at once is how this Mac gets taken down, so the
router may only claim the GPU when it is the *only* way a session survives.

Two consequences follow, and both are why triggers are as tight as they are:

  * An HTTP response — ANY status, including 429 and 529 — proves the network
    path works: the request reached Anthropic and Anthropic answered. A usage
    limit or a capacity blip is not a reachability problem, and serving it from
    a local 4B hid a real provider signal while taking the GPU. Hence
    :data:`FAILOVER_STATUSES` is empty by default.
  * A transport error proves only that *Anthropic* is unreachable, which is not
    the same as this host being offline (their edge or DNS can be down while
    everything else works). So reaching the threshold is necessary but not
    sufficient — :func:`internet_reachable` gets the final say.

Auth failures (401/403) were never triggers, for the same underlying reason:
the network path is fine and a credential is broken, and masking that behind a
local model would hide a revoked key indefinitely.

Breaker state is in-process (the router is one long-lived uvicorn process, and
a restart starting CLOSED is desirable), but it is also PUBLISHED to
:data:`STATE_PATH` on every transition so other local-GPU consumers can see
that the router has claimed Ollama. llm-jury reads that file and disables
itself rather than fighting for the same memory.
"""

import json
import logging
import os
import socket
import ssl
import subprocess
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# One router process can carry independent upstream breakers while sharing one
# local GPU. Publish their combined ownership so one recovery cannot hide a
# different upstream that is still serving from a local model.
_ACTIVE_BREAKERS: dict[Path, dict[str, str]] = {}


# Settings.failover_threshold and FailoverBreaker each carried a bare 1 with a
# comment asking the other to stay in step. One name, one place to change it.
DEFAULT_FAILOVER_THRESHOLD = 1


def _statuses_from_env() -> set[int]:
    raw = os.environ.get("BACKDOOR_FAILOVER_STATUSES", "").strip()
    if not raw:
        return set()
    out = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


# HTTP statuses that count toward opening the breaker. EMPTY BY DESIGN — see the
# module docstring: a status code is proof of reachability, not evidence against
# it. Set BACKDOOR_FAILOVER_STATUSES="429,529" to restore the old behavior.
FAILOVER_STATUSES: set[int] = _statuses_from_env()

# Where breaker state is published for other processes (llm-jury). Overridable so
# tests never touch the real file.
STATE_PATH = Path(
    os.environ.get("BACKDOOR_FAILOVER_STATE")
    or Path.home() / ".backdoor" / "failover-state.json"
)

# Literal IPs on purpose: the question is whether this MACHINE has a working
# route to the internet, and a hostname would fold a broken resolver into the
# answer. (A host that cannot resolve anything is offline for our purposes, but
# it should read as offline because of the probe, not because of a DNS timeout.)
#
# The third element is the name the probe's certificate must be valid for. It is
# passed as SNI and verified, never resolved, so the no-DNS property above still
# holds. See internet_reachable for why a TCP connection alone is not evidence.
CONNECTIVITY_PROBES = (
    ("1.1.1.1", 443, "one.one.one.one"),
    ("8.8.8.8", 443, "dns.google"),
)
CONNECTIVITY_TIMEOUT = 2.0

# How much more time the second handshake attempt gets after the first one runs
# out. See internet_reachable: a handshake that times out is the one probe
# outcome that does not distinguish a lying middlebox from a slow link, and the
# only thing that tells them apart is giving it more room and looking again.
CONNECTIVITY_SLOW_FACTOR = 4.0

# Escape hatch back to the old TCP-only probe. Exists because a false "offline"
# is not a harmless failure here — it claims the GPU and routes a session to a
# local model while the cloud was fine — so if TLS verification ever misbehaves
# on a network this was not tested against, it can be switched off without a
# deploy. Leaving it set re-opens the middlebox hole described below.
_TCP_ONLY_ENV = "BACKDOOR_PROBE_TCP_ONLY"

_tls_ctx: "ssl.SSLContext | None" = None


def _tls_context() -> ssl.SSLContext:
    """Default verifying context, built once. Certificates and hostname checked."""
    global _tls_ctx
    if _tls_ctx is None:
        _tls_ctx = ssl.create_default_context()
    return _tls_ctx


# What a single probe attempt concluded. Deliberately three-valued: "connected,
# then the handshake ran out of time" is NOT the same answer as "nothing is
# there" or "something answered and could not prove who it is", and folding it
# into either of those is the bug these constants exist to prevent.
_ONLINE = True        # a verified peer: this host has a working route out
_NO_ANSWER = False    # could not connect, or the peer proved it is an impostor
_INCONCLUSIVE = None  # connected, and then the handshake ran out of time


def _probe_once(
    host: str, port: int, cert_name: str, timeout: float, verify: bool
):
    """One connect-and-verify attempt against a single probe address.

    Returns :data:`_ONLINE`, :data:`_NO_ANSWER` or :data:`_INCONCLUSIVE`.

    The split between the two failure verdicts is the whole point. An
    `ssl.SSLError` is a *definite* answer — the peer spoke TLS and presented
    something the trust store rejects, which is what a captive portal or an
    untrusted MITM box does, and no amount of extra time changes it. A
    `TimeoutError` is not an answer at all: a silent acceptor produces it, and
    so does an honest peer on a link too slow to finish inside the budget.
    """
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        return _NO_ANSWER  # Nothing answered. Ordinary offline.

    with raw:
        if not verify:
            return _ONLINE
        # create_connection leaves its timeout on the socket, but the
        # handshake is the part that hangs against a silent acceptor, so
        # bound it explicitly rather than relying on that.
        raw.settimeout(timeout)
        try:
            with _tls_context().wrap_socket(raw, server_hostname=cert_name):
                return _ONLINE
        except ssl.SSLError as exc:
            # Answered, and proved it is someone else. Logged loudly because it
            # is the difference between "no network" and "something is answering
            # for the entire internet", and only one of those is a thing the
            # user can fix.
            logger.warning(
                "connectivity probe %s:%s answered with a certificate that is "
                "not %s (%s) — treating as offline; a captive portal or "
                "transparent middlebox answers exactly this way",
                host, port, cert_name, exc,
            )
            return _NO_ANSWER
        except OSError as exc:
            # Connected and then said nothing (TimeoutError), or died partway
            # through. Ambiguous by construction — see the caller.
            logger.debug(
                "connectivity probe %s:%s handshake did not finish in %.1fs (%s)",
                host, port, timeout, exc,
            )
            return _INCONCLUSIVE


def _probe_all(probes, timeout: float, verify: bool) -> bool:
    """True as soon as one probe proves a verified peer; False if none do.

    Every probe is tried before giving up, so one blackholed address does not
    read as a dead network. An inconclusive handshake gets one patient retry —
    see internet_reachable for why a timeout is not an answer.
    """
    patient = timeout * CONNECTIVITY_SLOW_FACTOR

    for host, port, cert_name in probes:
        verdict = _probe_once(host, port, cert_name, timeout, verify)
        if verdict is _INCONCLUSIVE:
            verdict = _probe_once(host, port, cert_name, patient, verify)
            if verdict is _INCONCLUSIVE:
                logger.warning(
                    "connectivity probe %s:%s accepted a connection but did not "
                    "prove it is %s within %.1fs — treating as offline; a captive "
                    "portal or transparent middlebox answers exactly this way",
                    host, port, cert_name, patient,
                )
                verdict = _NO_ANSWER
        if verdict is _ONLINE:
            return True
    return False


def _verify_enabled() -> bool:
    return os.environ.get(_TCP_ONLY_ENV, "").strip().lower() not in {"1", "true", "yes"}


def internet_reachable(
    probes=CONNECTIVITY_PROBES, timeout: float = CONNECTIVITY_TIMEOUT
) -> bool:
    """Can this host reach — and authenticate — a public address?

    Deliberately not an HTTP call to the upstream: by the time this runs we
    already know the upstream is unreachable. What is still unknown — and what
    decides whether claiming the GPU is justified — is whether anything else is.

    Returns True only on a verified peer; every probe failing means offline.

    **A completed TCP handshake is not evidence.** It was until 2026-08-17, and
    that was a silent hole: transparent middleboxes — captive portals, some
    corporate and hotel networks, certain VPN configurations — accept a
    connection to *any* address, including RFC 5737 addresses that are
    guaranteed unroutable. Measured on the network this repo is developed on,
    such a box completed a connection to 192.0.2.1:443 in ~0.2s. Against that,
    the old probe reported "online" with the internet gone, the breaker never
    opened, and failover silently did not happen — the one situation the whole
    mechanism exists for.

    So the probe completes a TLS handshake and verifies the certificate chain
    and hostname. A box that answers TCP without holding a certificate the
    system trust store accepts for `one.one.one.one` or `dns.google` cannot
    fake that.

    **A handshake timeout is not evidence either**, and treating it as one was
    its own silent hole — the mirror image of the first, and the reason this
    grew a second attempt on 2026-09-03. Until then every non-verifying outcome
    was read as the middlebox lie, timeouts included. On a congested link that
    is simply false: a tethered cellular connection measured on 2026-09-02 ran
    744 ms average RTT with 3.1 s spikes, which a 2.0 s handshake budget cannot
    survive, and the router opened the breaker three times that evening against
    a working internet. Every one of those opens logged `The handshake operation
    timed out`; not one logged a certificate error. A false open is expensive in
    exactly the way this module warns about — it claims the GPU, silently
    downgrades live sessions to a local model, and evicts the llm-jury council —
    so the ambiguous case gets a second, patient attempt at
    `CONNECTIVITY_SLOW_FACTOR` × the budget on a fresh connection.

    That keeps both properties. A genuinely silent acceptor never completes a
    handshake at any budget, so it still reads as offline; a real peer behind a
    stalling link now gets enough room to answer. The extra wait is paid only on
    the timeout path, and only on a request that is already failing.

    Set BACKDOOR_PROBE_TCP_ONLY=1 to fall back to the pre-2026-08-17 behavior.
    """
    return _probe_all(probes, timeout, _verify_enabled())


def service_reachable(url: str, timeout: float = CONNECTIVITY_TIMEOUT) -> bool:
    """Is the host behind `url` reachable, with a certificate that verifies?

    The narrower sibling of :func:`internet_reachable`, for a breaker whose
    upstream is a named service rather than "the internet". Same handshake, same
    patient retry, same verdicts — only the target differs.

    **This resolves DNS, and that is the point.** internet_reachable uses literal
    IPs precisely so a broken resolver cannot fold itself into the answer, because
    it is asking whether the machine has a route at all. This one is asking a
    different question — "is *that service* reachable from here" — and a name that
    will not resolve is a real way for a service to be unreachable, so the lookup
    belongs inside the answer rather than outside it.

    **What a True here does and does not prove.** It proves the host answers and
    holds a valid certificate for its own name. It says nothing about whether the
    service behind it will serve *you*: a usage limit, a quota reset, an expired
    token and a 503 all sit behind a perfectly reachable front door. So this may
    be used to reconsider a breaker that opened on a transport error, and never
    one that opened on an HTTP status. :meth:`FailoverBreaker.maybe_recover`
    enforces that distinction; this function is only the measurement.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(url if "//" in url else f"//{url}", scheme="https")
    host = parts.hostname
    if not host:
        logger.warning("service probe cannot parse a host out of %r", url)
        return False
    port = parts.port or (80 if parts.scheme == "http" else 443)
    # Plain HTTP has no certificate to verify, so a handshake would be the wrong
    # test; fall back to the TCP-only verdict rather than reporting False.
    verify = _verify_enabled() and parts.scheme != "http"
    return _probe_all(((host, port, host),), timeout, verify)


def _notify(title: str, message: str) -> None:
    """Best-effort macOS notification (router runs in the gui launchd domain)."""
    try:
        script = 'display notification "{}" with title "{}"'.format(
            message.replace('"', "'"), title.replace('"', "'")
        )
        subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:  # notification is decoration, never let it break routing
        pass


class FailoverBreaker:
    def __init__(
        self,
        # See Settings.failover_threshold for why 1 rather than 2: the
        # connectivity probe, not the count, is what prevents a transient blip
        # from claiming the GPU.
        threshold: int = DEFAULT_FAILOVER_THRESHOLD,
        window: float = 120.0,
        probe_interval: float = 60.0,
        now_fn: Callable[[], float] = time.monotonic,
        notify_fn: Callable[[str, str], None] = _notify,
        online_fn: Callable[[], bool] = internet_reachable,
        state_path: Path | None = None,
        source: str = "anthropic",
        upstream_name: str = "Anthropic",
        require_offline: bool = True,
        # Deliberately inert by default, unlike `threshold` above. These two are
        # policy — how long to tolerate a broken upstream, and how often to
        # interrupt the human — and the router sets them from
        # Settings.failover_min_outage_seconds (30s) and
        # Settings.failover_notify_cooldown_seconds (900s). A zero default keeps
        # this class a pure state machine that opens the moment its inputs say
        # to, which is what the mechanics tests are about; the wiring is pinned
        # separately in tests/test_failover_recovery_wiring.py so a breaker
        # constructed without the policy cannot reach production unnoticed.
        min_outage: float = 0.0,
        notify_cooldown: float = 0.0,
        # Reachability probe for THIS breaker's own upstream, used to reconsider
        # a service-level breaker. Injected like online_fn so the state machine
        # stays testable with no transport. See maybe_recover.
        service_fn: "Callable[[], bool] | None" = None,
    ):
        self.threshold = threshold
        self.window = window
        self.probe_interval = probe_interval
        self._now = now_fn
        self._notify = notify_fn
        self._online = online_fn
        self._state_path = STATE_PATH if state_path is None else state_path
        self.source = source
        self.upstream_name = upstream_name
        self.require_offline = require_offline
        self.min_outage = min_outage
        self.notify_cooldown = notify_cooldown
        self._last_open_notice_at: float | None = None
        # Whether the CURRENT open episode was announced. A close speaks only if
        # its open did, so a cooldown never leaves an orphan "back to cloud".
        self._announced = False
        self._service = service_fn
        # Whether the failure that opened the breaker was a transport error, as
        # opposed to an HTTP status the upstream deliberately returned. Only the
        # former is something a reachability probe can disprove.
        self._opened_on_transport = True
        self.open = False
        self.reason = ""
        self._failures = 0
        self._first_failure_at = 0.0
        self._last_probe_at = 0.0
        # Deliberately separate from _last_probe_at. That one rations half-open
        # attempts at real upstream traffic; this one rations the standalone
        # recovery probe. Sharing a single timer would let whichever ran first
        # starve the other, which is the opposite of the point.
        self._last_recovery_at = 0.0
        # (provider_base_url, model) pairs this breaker has caused to be loaded.
        # Held so closing can hand them back — see note_claim/drain_claims.
        self._claims: set[tuple[str, str]] = set()
        self._publish()

    def _announce(self, now: float, message: str) -> None:
        """Notify the human, at most once per `notify_cooldown` per breaker.

        Every transition is still logged; this only rations the desktop popup.
        A flapping link produced sixteen of them on the evening of 2026-09-02,
        which is noise rather than information — and the recovery ticker makes
        transitions more frequent, not less, so the rationing has to exist for it
        to be an improvement.
        """
        if (
            self._last_open_notice_at is not None
            and (now - self._last_open_notice_at) < self.notify_cooldown
        ):
            self._announced = False
            return
        self._last_open_notice_at = now
        self._announced = True
        self._notify(f"Backdoor {self.source} failover", message)

    def _publish(self) -> None:
        """Write breaker state where other local-GPU consumers can read it.

        Best-effort: a router that cannot write this file must still route. The
        reader (llm-jury) treats a missing/unreadable file as "not failing over",
        which is the same default a fresh router has.
        """
        try:
            active = _ACTIVE_BREAKERS.setdefault(self._state_path, {})
            if self.open:
                active[self.source] = self.reason
            else:
                active.pop(self.source, None)
            reasons = dict(sorted(active.items()))
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "failover_active": bool(reasons),
                "reason": next(iter(reasons.values()), ""),
                "active_sources": list(reasons),
                "reasons": reasons,
                "updated_at": time.time(),
                "pid": os.getpid(),
            }
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self._state_path)  # atomic: readers never see a partial file
        except OSError:
            pass

    def allow_upstream(self) -> bool:
        """Should this request attempt the real API? Always yes while CLOSED;
        while OPEN, yes once per probe interval (half-open)."""
        if not self.open:
            return True
        now = self._now()
        if (now - self._last_probe_at) >= self.probe_interval:
            self._last_probe_at = now
            return True
        return False

    def record_failure(self, reason: str, *, transport_error: bool = True) -> bool:
        """Record a trigger-class upstream failure. Returns True when the
        caller should serve THIS request locally (breaker open, including the
        request whose failure just opened it).

        `transport_error=False` marks a failure the upstream *answered* with — an
        HTTP status such as 429 or 503. The distinction is not cosmetic: a
        reachability probe can disprove "I could not reach it" and can never
        disprove "it told me no", so it is what decides whether
        :meth:`maybe_recover` may act on a service-level breaker.
        """
        now = self._now()
        if self._failures == 0 or (now - self._first_failure_at) > self.window:
            self._failures = 1
            self._first_failure_at = now
        else:
            self._failures += 1
        self.reason = reason
        sustained = (now - self._first_failure_at) >= self.min_outage
        if not self.open and self._failures >= self.threshold and sustained:
            # Anthropic requires a genuine internet outage before it may claim
            # the GPU. Other upstreams can opt into service-level failover for
            # explicit transient statuses such as usage limits.
            if not self.require_offline or not self._online():
                self.open = True
                self._opened_on_transport = transport_error
                self._last_probe_at = now
                self._last_recovery_at = now
                logger.warning(
                    "failover OPEN after %d consecutive failures (%s) and no "
                    "usable %s service — serving traffic from the local "
                    "failover profile",
                    self._failures, reason, self.upstream_name,
                )
                self._announce(
                    now,
                    f"{self.upstream_name} unavailable ({reason}); routing to local model",
                )
                self._publish()
            else:
                # Online: relay the error so the caller sees the real failure.
                # Reset the count so the probe is not re-run on every request —
                # it takes another `threshold` failures to ask again.
                logger.warning(
                    "%s failing (%s) but this host is online — relaying the "
                    "error instead of failing over (the local GPU stays free)",
                    self.upstream_name, reason,
                )
                self._failures = 0
        return self.open

    def note_claim(self, provider_base_url: str, model: str) -> None:
        """Record that failing over caused `model` to be loaded locally.

        Only meaningful while OPEN. A deliberate `/model qwen` route loads a
        tier too, but the user asked for that one and it is not ours to evict —
        the caller is responsible for only reporting failover-path loads.
        """
        if model:
            self._claims.add((provider_base_url, model))

    def drain_claims(self) -> set[tuple[str, str]]:
        """Take the claimed tiers, clearing them. Caller does the unloading.

        Returning the work instead of doing it keeps this module free of HTTP:
        the breaker is pure decision logic, tested with a fake clock and no I/O,
        and adding a network call to it would make every state-machine test
        need a transport.
        """
        claims, self._claims = self._claims, set()
        return claims

    def _close(self, log_line: str, note: str) -> None:
        """Return to CLOSED and reset the counters. Idempotent when already closed.

        Closing leaves the claimed local tiers still resident — the caller must
        drain_claims() and unload them. This is the moment that matters for
        memory: the breaker closing is the exact point the GPU is no longer
        needed, and waiting for Ollama's idle timer instead left ~9.7 GB wired
        for ~9 minutes past it on 2026-08-24.
        """
        was_open = self.open
        if was_open:
            logger.warning(log_line)
            if self._announced:
                self._notify(f"Backdoor {self.source} failover", note)
                self._announced = False
        self.open = False
        self.reason = ""
        self._failures = 0
        if was_open:
            self._publish()

    def record_success(self) -> None:
        """Any non-trigger upstream response: reset, and close if OPEN."""
        self._close(
            f"failover CLOSED — {self.upstream_name} reachable again",
            f"{self.upstream_name} recovered; back to cloud",
        )

    def maybe_recover(self) -> bool:
        """Close an OPEN breaker whose outage has ended, with no request to ride on.

        Returns True only when this call closed it, so the caller knows to
        release the local tiers (see record_success).

        **Why this exists.** Until 2026-09-03 recovery had exactly one path:
        `allow_upstream` hands a real `/v1/messages` passthrough a half-open slot
        once per `probe_interval`, and only that request's success closes the
        breaker. Every other route that talks upstream (`/v1/messages/count_tokens`
        and friends) deliberately never calls `record_success`, because closing
        obliges the caller to unload the tiers and only the messages path knows
        how. So recovery needed a rider, and the outage itself is what removes
        the riders: sessions being served by a local 4B generate far less traffic,
        and a user who walks away from a degraded session generates none.

        Measured 2026-09-02: the breaker opened at 22:28:14 and did not close
        until 23:45:51 — 77 minutes 37 seconds, of which the network was healthy
        for most — and it closed at the exact second unrelated test traffic
        arrived. For that whole window every routed session was quietly served by
        qwen instead of the cloud model.

        **Each breaker is answered by the probe that matches its premise.**

        An offline-gated breaker (`require_offline=True`, the Claude upstream)
        opened because this host had no route out, so `online_fn` is the exact
        negation of what it concluded.

        A service-level breaker (`require_offline=False`, the Codex upstream)
        never consulted connectivity at all, so that probe answers nothing about
        it. It is answered by `service_fn` — a verified handshake to its own
        upstream host — and then only when it opened on a **transport error**.
        That condition is the whole care of this branch: reachability can
        disprove "I could not reach it", and can never disprove "it answered me
        with 429". A breaker that opened on a status keeps the half-open path as
        its only route back, because reaching the service is the only thing that
        settles a quota, and the request that does it carries the caller's own
        credentials — which this router relays and never holds, so it could not
        ask on its own behalf even if a status were worth re-testing.

        The probe is the same one that opened the breaker, so the close is the
        exact negation of the open: the breaker means "this host is offline", and
        the moment that stops being true the premise is gone. If Anthropic itself
        is still down, the next real request fails, `record_failure` re-runs the
        probe, finds the host online, and relays the error — which is the
        documented behaviour for an upstream outage on a working link.
        """
        if not self.open:
            return False
        if not self.require_offline and (
            self._service is None or not self._opened_on_transport
        ):
            # Nothing this can measure would change the answer. Checked before
            # the timer so a permanently ineligible breaker does no work at all.
            return False
        now = self._now()
        if (now - self._last_recovery_at) < self.probe_interval:
            return False
        self._last_recovery_at = now

        if self.require_offline:
            if not self._online():
                return False
            log_line = "failover CLOSED — this host is back online (recovery probe)"
            note = "Back online — returning to cloud"
        else:
            if not self._service():
                return False
            log_line = (
                f"failover CLOSED — {self.upstream_name} is reachable again "
                f"(service probe)"
            )
            note = f"{self.upstream_name} reachable again; returning to cloud"

        self._close(log_line, note)
        return True
