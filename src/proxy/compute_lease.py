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
