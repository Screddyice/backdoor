"""Behaviour of scripts/deploy-router.sh that is worth pinning.

The quiet-window wait in step 2 exists so a restart does not kill in-flight
requests. A DRY_RUN restarts nothing, so making it sit through that wait buys
nothing — and on a machine where a Claude session is writing to the router log
every few seconds, the window never opens at all. On 2026-09-04 that turned a
"show me the plan" into a 3-minute wait ending in
`ABORT: still busy after 180s`, with the plan never printed.
"""

import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy-router.sh"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True,
    )


@pytest.fixture
def service_checkout(tmp_path: Path) -> Path:
    """A checkout one commit behind its origin, the shape a deploy expects."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    svc = tmp_path / "svc"

    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(seed)],
                   check=True, capture_output=True)
    (seed / "file").write_text("one\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "one")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main")

    subprocess.run(["git", "clone", "-q", str(origin), str(svc)],
                   check=True, capture_output=True)

    (seed / "file").write_text("two\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "two")
    _git(seed, "push", "-q", "origin", "main")
    return svc


def _run(svc: Path, log: Path, **env_extra: str) -> subprocess.CompletedProcess:
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": str(svc.parent),
        "RESTART_CMD": "true",
        "ROUTER_LOG": str(log),
        # Never the live router. The default is http://127.0.0.1:8083/health,
        # and a test suite has no business reaching the process this repo's
        # live-control boundary exists to keep agents away from.
        "ROUTER_HEALTH": "http://127.0.0.1:1/health",
        **env_extra,
    }
    return subprocess.run(
        ["bash", str(SCRIPT), str(svc), "origin/main"],
        env=env, capture_output=True, text=True, timeout=60,
    )


def test_a_dry_run_does_not_sit_through_the_quiet_window(service_checkout, tmp_path):
    log = tmp_path / "router.log"
    log.write_text("busy\n")

    result = _run(service_checkout, log, DRY_RUN="1")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "dry run complete" in result.stdout
    assert "waiting for the router to go idle" not in result.stdout
    assert "would fast-forward" in result.stdout


def test_a_real_run_still_waits_for_the_quiet_window(service_checkout, tmp_path):
    log = tmp_path / "router.log"
    log.write_text("quiet\n")

    result = _run(service_checkout, log, QUIET_SECONDS="1", QUIET_TIMEOUT="10")

    # Only that the wait happens. What comes after it needs a live router, and
    # this test deliberately points ROUTER_HEALTH at a closed port instead.
    assert "waiting for the router to go idle" in result.stdout
    assert "idle, proceeding" in result.stdout
