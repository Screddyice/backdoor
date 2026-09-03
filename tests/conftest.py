import pytest
"""Shared test isolation.

The failover breaker publishes its state to a real path (`~/.backdoor/
failover-state.json` by default) so other processes — notably llm-jury — can see
that the router has claimed the local GPU. Anything that builds a breaker
therefore writes to the developer's home directory unless redirected, and
`test_relay_encoding` builds a whole app, which builds a breaker.

Redirect it for every test. Writing real state from a test run is not just
untidy: llm-jury reads that file to decide whether to stand down, so a stray
`failover_active: true` from a suite would disable the council on the machine
that ran the tests.
"""

import pytest

from src.proxy import compute_lease, failover


@pytest.fixture(autouse=True)
def _isolate_failover_state(tmp_path, monkeypatch):
    monkeypatch.setattr(
        failover, "STATE_PATH", tmp_path / "failover-state.json"
    )
    monkeypatch.setattr(compute_lease, "LEASE_DIR", tmp_path / "compute-leases")


@pytest.fixture(autouse=True)
def _no_live_mlx_probe(monkeypatch):
    """Keep routing tests off the real MLX server.

    mlx_admin.resolve_profile probes or stops 127.0.0.1:8080 when a 27B route
    changes runtimes. That made every routing assertion depend on whether a
    server happened to be running on this machine.

    Identity here, so tests assert what the router decided rather than what the
    host was doing. Tests that care about the fallback patch it themselves.
    """
    from src.proxy import mlx_admin

    async def _identity(profile: str) -> str:
        return profile

    monkeypatch.setattr(mlx_admin, "resolve_profile", _identity)
