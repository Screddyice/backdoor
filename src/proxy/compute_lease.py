"""Best-effort ownership leases for exclusive local models.

One file per router process and source avoids cross-process read/modify/write races.
Consumers ignore expired leases and leases whose writer no longer exists.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path


LEASE_DIR = Path(
    os.environ.get("BACKDOOR_COMPUTE_LEASE_DIR", "")
    or Path.home() / ".backdoor" / "compute-leases"
)


def _safe_source(source: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", source).strip("-.")


def claim_exclusive_model(
    model: str, *, source: str, ttl_seconds: float
) -> Path | None:
    """Publish an exclusive-model lease without delaying the inference path."""

    safe_source = _safe_source(source)
    if not model or not safe_source or ttl_seconds <= 0:
        return None
    try:
        now = time.time()
        LEASE_DIR.mkdir(parents=True, exist_ok=True)
        path = LEASE_DIR / f"{os.getpid()}-{safe_source}.json"
        payload = {
            "active": True,
            "model": model,
            "source": source,
            "expires_at": now + ttl_seconds,
            "updated_at": now,
            "pid": os.getpid(),
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
        return path
    except OSError:
        return None


def _process_alive(pid) -> bool:
    """Is `pid` still running? An unreadable pid counts as gone."""
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def foreign_exclusive_lease(*, own_pid: int | None = None, now: float | None = None):
    """An exclusive local-GPU lease held by some OTHER live process, or None.

    The router publishes leases for tiers it loads (see `claim_exclusive_model`)
    and never read anyone else's, which left one real hole: a deliberate `qwen`
    session loads the 27B -- roughly 17 GB resident on a 36 GB host -- and
    publishes a lease so other local-compute consumers stand down. A network
    drop during that session would have failed Claude and Codex over and loaded
    a SECOND model on top of the first.

    Fails OPEN. A missing directory is the ordinary case, and a lease that
    cannot be read is not evidence that the GPU is busy -- refusing to fail over
    because a file was unparseable would break the feature to protect memory
    that may well be free.

    Ignored: our own pid (the router's tiers are not a reason to refuse), leases
    past `expires_at`, `active: false`, and leases whose process is gone -- a
    crashed session must not strand failover forever.
    """
    own = os.getpid() if own_pid is None else own_pid
    moment = time.time() if now is None else now
    try:
        entries = sorted(LEASE_DIR.glob("*.json"))
    except OSError:
        return None
    for entry in entries:
        try:
            lease = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue  # one unreadable lease is not a verdict about the GPU
        if not isinstance(lease, dict) or not lease.get("active"):
            continue
        try:
            if float(lease.get("expires_at", 0)) <= moment:
                continue
        except (TypeError, ValueError):
            continue
        pid = lease.get("pid")
        if pid == own or not _process_alive(pid):
            continue
        return lease
    return None
