"""Failover must stand down while another process owns the local GPU.

A deliberate `qwen` session loads the 27B — about 17 GB resident on a 36 GB
host — and publishes an exclusive lease so other local-compute consumers stand
down. The router publishes leases of its own but never read anyone else's, so a
network drop during such a session would have failed Claude and Codex over and
loaded a SECOND model on top of the first. Serving the turn is not worth
wedging the machine, and the honest answer is the transport error.
"""

import json
import os
import time

from src.proxy import compute_lease


def _write(dirpath, name, **fields):
    payload = {"active": True, "model": "qwen3.8:27b-obliterated", "source": "qwen",
               "pid": os.getpid(), "expires_at": time.time() + 600}
    payload.update(fields)
    p = dirpath / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_a_live_foreign_lease_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(compute_lease, "LEASE_DIR", tmp_path)
    _write(tmp_path, "qwen-1.json", pid=os.getpid())
    held = compute_lease.foreign_exclusive_lease(own_pid=os.getpid() + 1)
    assert held is not None
    assert held["model"] == "qwen3.8:27b-obliterated"


def test_our_own_lease_is_not_foreign(tmp_path, monkeypatch):
    """The router holds leases for the tiers it loads; those must not block it."""
    monkeypatch.setattr(compute_lease, "LEASE_DIR", tmp_path)
    _write(tmp_path, "router-1.json", pid=os.getpid(), source="router")
    assert compute_lease.foreign_exclusive_lease(own_pid=os.getpid()) is None


def test_an_expired_lease_does_not_block(tmp_path, monkeypatch):
    monkeypatch.setattr(compute_lease, "LEASE_DIR", tmp_path)
    _write(tmp_path, "qwen-2.json", pid=os.getpid(), expires_at=time.time() - 1)
    assert compute_lease.foreign_exclusive_lease(own_pid=os.getpid() + 1) is None


def test_a_lease_from_a_dead_process_does_not_block(tmp_path, monkeypatch):
    """A session that crashed must not strand failover forever."""
    monkeypatch.setattr(compute_lease, "LEASE_DIR", tmp_path)
    _write(tmp_path, "qwen-3.json", pid=999_999_000)
    assert compute_lease.foreign_exclusive_lease(own_pid=os.getpid()) is None


def test_inactive_lease_does_not_block(tmp_path, monkeypatch):
    monkeypatch.setattr(compute_lease, "LEASE_DIR", tmp_path)
    _write(tmp_path, "qwen-4.json", active=False)
    assert compute_lease.foreign_exclusive_lease(own_pid=os.getpid() + 1) is None


def test_unreadable_lease_dir_never_blocks(tmp_path, monkeypatch):
    """Fail OPEN on a read error: a missing directory is the normal case."""
    monkeypatch.setattr(compute_lease, "LEASE_DIR", tmp_path / "nope")
    assert compute_lease.foreign_exclusive_lease(own_pid=os.getpid()) is None


def test_garbage_lease_is_skipped_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(compute_lease, "LEASE_DIR", tmp_path)
    (tmp_path / "bad.json").write_text("not json", encoding="utf-8")
    _write(tmp_path, "qwen-5.json", pid=os.getpid())
    assert compute_lease.foreign_exclusive_lease(own_pid=os.getpid() + 1) is not None


# --- the gate in the request path -------------------------------------------

import httpx  # noqa: E402
import pytest  # noqa: E402

import src.proxy.routes as routes  # noqa: E402
from src.proxy.config import Settings  # noqa: E402
from src.proxy.failover import FailoverBreaker  # noqa: E402


class _Req:
    def __init__(self):
        self.method = "POST"
        self.headers = {"content-type": "application/json"}
        self.url = httpx.URL("http://127.0.0.1:8083/v1/messages")


class _DeadUpstream:
    def build_request(self, method, url, *, content, headers):
        return httpx.Request(method, f"https://api.anthropic.com{url}",
                             content=content, headers=headers)

    async def send(self, request, *, stream):
        raise httpx.ConnectError("refused")

    async def aclose(self):
        pass


def _breaker(tmp_path):
    return FailoverBreaker(threshold=1, require_offline=False, online_fn=lambda: False,
                           notify_fn=lambda *_: None, min_outage=0.0,
                           state_path=tmp_path / "state.json")


@pytest.mark.asyncio
async def test_failover_declines_while_another_process_holds_the_gpu(tmp_path, monkeypatch):
    """The whole point: a qwen session's 27B must not get a second model on top."""
    leases = tmp_path / "leases"
    leases.mkdir()
    _write(leases, "qwen-99.json", pid=os.getpid())
    monkeypatch.setattr(compute_lease, "LEASE_DIR", leases)
    monkeypatch.setattr(compute_lease, "foreign_exclusive_lease",
                        lambda **kw: {"source": "qwen", "pid": 4242,
                                      "model": "qwen3.8:27b-obliterated"})
    upstream = _DeadUpstream()
    monkeypatch.setattr(routes, "_get_upstream", lambda settings: upstream)
    br = _breaker(tmp_path)
    monkeypatch.setattr(routes, "get_breaker", lambda settings: br)

    with pytest.raises(routes.HTTPException) as caught:
        await routes._try_upstream(_Req(), b"{}", Settings(router_mode="hybrid"))
    assert caught.value.status_code == 502
    assert "failover declined" in caught.value.detail.lower()
    assert "qwen" in caught.value.detail.lower()


@pytest.mark.asyncio
async def test_failover_proceeds_when_the_gpu_is_free(tmp_path, monkeypatch):
    """With no foreign lease the behaviour is unchanged: serve locally."""
    monkeypatch.setattr(compute_lease, "foreign_exclusive_lease", lambda **kw: None)
    upstream = _DeadUpstream()
    monkeypatch.setattr(routes, "_get_upstream", lambda settings: upstream)
    br = _breaker(tmp_path)
    monkeypatch.setattr(routes, "get_breaker", lambda settings: br)

    result = await routes._try_upstream(_Req(), b"{}", Settings(router_mode="hybrid"))
    assert result is None, "None means 'serve this turn from the local profile'"
