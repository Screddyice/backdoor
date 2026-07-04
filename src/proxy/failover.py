"""Cloud→local failover breaker for hybrid mode.

When the real Anthropic API stops working — network gone, usage limit hit,
overloaded — hybrid-mode passthrough requests are served by a local Ollama
profile instead of failing, so an in-flight Claude Code session keeps going.

Shape: a classic circuit breaker.

  CLOSED  every passthrough request goes upstream. A trigger-class failure
          (transport error, or HTTP status in the failover set) increments a
          consecutive-failure counter; any non-trigger response resets it.
          Errors below the threshold are relayed verbatim so the client's own
          retry logic still runs.
  OPEN    reached after `threshold` consecutive failures inside `window`
          seconds. Passthrough-bound /v1/messages requests are served by the
          failover profile. One request per `probe_interval` is allowed to try
          upstream (half-open); a non-trigger response closes the breaker.

Auth failures (401/403) are deliberately NOT triggers: they mean the network
path is fine and a credential is broken — masking that behind a local model
would hide a revoked key indefinitely.

State is in-process only: the router is one long-lived uvicorn process, and a
restart starting CLOSED is the desired behavior anyway.
"""

import logging
import subprocess
import time
from typing import Callable

logger = logging.getLogger(__name__)

# Statuses that count toward opening the breaker. 429 covers both transient
# rate limits and hard usage limits — a transient one never reaches the
# threshold because the client's spaced retries succeed and reset the count.
FAILOVER_STATUSES = {429, 500, 502, 503, 529}


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
        threshold: int = 3,
        window: float = 120.0,
        probe_interval: float = 60.0,
        now_fn: Callable[[], float] = time.monotonic,
        notify_fn: Callable[[str, str], None] = _notify,
    ):
        self.threshold = threshold
        self.window = window
        self.probe_interval = probe_interval
        self._now = now_fn
        self._notify = notify_fn
        self.open = False
        self.reason = ""
        self._failures = 0
        self._first_failure_at = 0.0
        self._last_probe_at = 0.0

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
            self.open = True
            self._last_probe_at = now
            logger.warning(
                "failover OPEN after %d consecutive failures (%s) — serving "
                "passthrough traffic from the local failover profile",
                self._failures, reason,
            )
            self._notify(
                "Backdoor failover",
                f"Anthropic unreachable ({reason}) — routing to local model",
            )
        return self.open

    def record_success(self) -> None:
        """Any non-trigger upstream response: reset, and close if OPEN."""
        if self.open:
            logger.warning("failover CLOSED — Anthropic reachable again")
            self._notify("Backdoor failover", "Anthropic recovered — back to cloud")
        self.open = False
        self.reason = ""
        self._failures = 0
