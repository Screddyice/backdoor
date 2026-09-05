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
from src.proxy.config import Settings


@pytest.fixture(autouse=True, scope="session")
def _ignore_the_developer_s_env_file():
    """Read no `.env`, so a local run and CI cannot disagree.

    `Settings` declares `env_file=".env"`, which pydantic resolves against the
    CURRENT WORKING DIRECTORY. A checkout with a real `.env` — every machine
    that has ever run the router from its source tree — therefore feeds live
    values into every `Settings()` a test builds, while CI reads nothing.

    That is not hypothetical. On 2026-09-05 two eviction tests failed locally
    and passed in CI on the same commit: the repo `.env` sets
    PROVIDER_BASE_URL to the local Ollama, which turned on the window guard in
    tests written for a hosted provider, which escalated a session that was
    asserting it would not escalate. Twenty minutes went into the wrong
    question, and the answer was a file that is not even tracked.

    Profile loading is unaffected: `load_profile_settings` passes its path as
    `_env_file` per call, which overrides this.
    """
    original = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None
    try:
        yield
    finally:
        Settings.model_config["env_file"] = original


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


@pytest.fixture(autouse=True)
def _no_leaked_dns_cache():
    """Keep the resolver wrapper out of every test that did not ask for it.

    `install()` rebinds `socket.getaddrinfo` for the whole process, so a test
    that builds the app leaves the wrapper in place for everything that runs
    after it — including tests that patch `socket.getaddrinfo` and would then be
    talking to the real resolver without noticing.
    """
    yield
    from src.proxy import resolver

    resolver.uninstall()
