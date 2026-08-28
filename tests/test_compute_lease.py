import json
import os
from pathlib import Path

from src.proxy import compute_lease


def test_claim_exclusive_model_publishes_process_scoped_lease(tmp_path, monkeypatch):
    monkeypatch.setattr(compute_lease, "LEASE_DIR", tmp_path)
    monkeypatch.setattr(compute_lease.time, "time", lambda: 1_000.0)

    path = compute_lease.claim_exclusive_model(
        "qwen3.8:27b-obliterated", source="claude-explicit", ttl_seconds=600
    )

    assert path == tmp_path / f"{os.getpid()}-claude-explicit.json"
    assert json.loads(path.read_text()) == {
        "active": True,
        "model": "qwen3.8:27b-obliterated",
        "source": "claude-explicit",
        "expires_at": 1_600.0,
        "updated_at": 1_000.0,
        "pid": os.getpid(),
    }


def test_claim_exclusive_model_rejects_empty_or_nonpositive_lease(tmp_path, monkeypatch):
    monkeypatch.setattr(compute_lease, "LEASE_DIR", tmp_path)

    assert compute_lease.claim_exclusive_model("", source="claude", ttl_seconds=600) is None
    assert compute_lease.claim_exclusive_model("model", source="", ttl_seconds=600) is None
    assert compute_lease.claim_exclusive_model("model", source="claude", ttl_seconds=0) is None
    assert not list(Path(tmp_path).iterdir())
