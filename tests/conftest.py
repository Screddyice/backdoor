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

from src.proxy import failover


@pytest.fixture(autouse=True)
def _isolate_failover_state(tmp_path, monkeypatch):
    monkeypatch.setattr(
        failover, "STATE_PATH", tmp_path / "failover-state.json"
    )
