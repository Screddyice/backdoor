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
          (half-open); a success closes the breaker.

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


def internet_reachable(
    probes=CONNECTIVITY_PROBES, timeout: float = CONNECTIVITY_TIMEOUT
) -> bool:
    """Can this host open a TCP connection to a public address?

    Deliberately not an HTTP call to Anthropic: by the time this runs we already
    know Anthropic is unreachable. What is still unknown — and what decides
    whether claiming the GPU is justified — is whether anything else is.

    Fails CLOSED (returns True, "we are online") only on a real connection; any
    error on every probe means offline. Both probes are tried before giving up,
    so one blackholed resolver does not read as a dead network.

    **A completed TCP handshake is not evidence.** It was until 2026-08-17, and
    that was a silent hole: transparent middleboxes — captive portals, some
    corporate and hotel networks, certain VPN configurations — accept a
    connection to *any* address, including RFC 5737 addresses that are
    guaranteed unroutable. Measured on the network this repo is developed on,
    such a box completed a connection to 192.0.2.1:443 in ~0.2s. Against that,
    the old probe reported "online" with the internet gone, the breaker never
    opened, and failover silently did not happen — the one situation the whole
    mechanism exists for.

    So the probe now completes a TLS handshake and verifies the certificate
    chain and hostname. A box that answers TCP without holding a certificate
    the system trust store accepts for `one.one.one.one` or `dns.google` cannot
    fake that. A corporate MITM proxy whose CA is installed on this machine
    still can, but such a proxy is generally forwarding traffic, so "online" is
    then the right answer anyway.

    Set BACKDOOR_PROBE_TCP_ONLY=1 to fall back to the pre-2026-08-17 behavior.
    """
    verify = os.environ.get(_TCP_ONLY_ENV, "").strip().lower() not in {"1", "true", "yes"}

    for host, port, cert_name in probes:
        try:
            raw = socket.create_connection((host, port), timeout=timeout)
        except OSError:
            continue  # Nothing answered. Ordinary offline.

        with raw:
            if not verify:
                return True
            # create_connection leaves its timeout on the socket, but the
            # handshake is the part that hangs against a silent acceptor, so
            # bound it explicitly rather than relying on that.
            raw.settimeout(timeout)
            try:
                with _tls_context().wrap_socket(raw, server_hostname=cert_name):
                    return True
            except OSError as exc:
                # Connected, then could not prove who it was. Deliberately catches
                # every OSError rather than ssl.SSLError alone, because the two
                # middlebox flavours fail differently: one speaks TLS with an
                # untrusted certificate (SSLError), the other accepts the socket
                # and says nothing at all (timeout). Both are the same lie.
                #
                # Logged loudly because it is the difference between "no network"
                # and "something is answering for the entire internet", and only
                # one of those is a thing the user can fix.
                logger.warning(
                    "connectivity probe %s:%s accepted a connection but did not "
                    "prove it is %s (%s) — treating as offline; a captive portal "
                    "or transparent middlebox answers exactly this way",
                    host, port, cert_name, exc,
                )
                continue
    return False


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
        # Keep in step with Settings.failover_threshold — see the note there for
        # why 2 rather than 3 (the connectivity probe, not the count, is what
        # prevents a transient blip from claiming the GPU).
        threshold: int = 2,
        window: float = 120.0,
        probe_interval: float = 60.0,
        now_fn: Callable[[], float] = time.monotonic,
        notify_fn: Callable[[str, str], None] = _notify,
        online_fn: Callable[[], bool] = internet_reachable,
        state_path: Path | None = None,
    ):
        self.threshold = threshold
        self.window = window
        self.probe_interval = probe_interval
        self._now = now_fn
        self._notify = notify_fn
        self._online = online_fn
        self._state_path = STATE_PATH if state_path is None else state_path
        self.open = False
        self.reason = ""
        self._failures = 0
        self._first_failure_at = 0.0
        self._last_probe_at = 0.0
        # (provider_base_url, model) pairs this breaker has caused to be loaded.
        # Held so closing can hand them back — see note_claim/drain_claims.
        self._claims: set[tuple[str, str]] = set()
        self._publish()

    def _publish(self) -> None:
        """Write breaker state where other local-GPU consumers can read it.

        Best-effort: a router that cannot write this file must still route. The
        reader (llm-jury) treats a missing/unreadable file as "not failing over",
        which is the same default a fresh router has.
        """
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "failover_active": self.open,
                "reason": self.reason,
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

    def record_failure(self, reason: str) -> bool:
        """Record a trigger-class upstream failure. Returns True when the
        caller should serve THIS request locally (breaker open, including the
        request whose failure just opened it)."""
        now = self._now()
        if self._failures == 0 or (now - self._first_failure_at) > self.window:
            self._failures = 1
            self._first_failure_at = now
        else:
            self._failures += 1
        self.reason = reason
        if not self.open and self._failures >= self.threshold:
            # Threshold reached, but that only establishes that ANTHROPIC is
            # unreachable. Claiming the local GPU is justified only when nothing
            # else is reachable either — otherwise we would hide a provider
            # outage behind a 4B and evict the llm-jury council to do it.
            if not self._online():
                self.open = True
                self._last_probe_at = now
                logger.warning(
                    "failover OPEN after %d consecutive failures (%s) and no "
                    "internet — serving passthrough traffic from the local "
                    "failover profile",
                    self._failures, reason,
                )
                self._notify(
                    "Backdoor failover",
                    f"Offline ({reason}) — routing to local model",
                )
                self._publish()
            else:
                # Online: relay the error so the caller sees the real failure.
                # Reset the count so the probe is not re-run on every request —
                # it takes another `threshold` failures to ask again.
                logger.warning(
                    "upstream failing (%s) but this host is online — relaying the "
                    "error instead of failing over (the local GPU stays free)",
                    reason,
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

    def record_success(self) -> None:
        """Any non-trigger upstream response: reset, and close if OPEN.

        Closing leaves the claimed local tiers still resident — the caller must
        drain_claims() and unload them. This is the moment that matters for
        memory: the breaker closing is the exact point the GPU is no longer
        needed, and waiting for Ollama's idle timer instead left ~9.7 GB wired
        for ~9 minutes past it on 2026-08-24.
        """
        was_open = self.open
        if was_open:
            logger.warning("failover CLOSED — Anthropic reachable again")
            self._notify("Backdoor failover", "Anthropic recovered — back to cloud")
        self.open = False
        self.reason = ""
        self._failures = 0
        if was_open:
            self._publish()
