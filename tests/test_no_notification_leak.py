"""A test run must not put notifications on the developer's screen.

Regression guard for a side effect that escaped the process: breakers built
without an explicit `notify_fn` used the real osascript notifier, so running the
suite announced outages that never happened.
"""

import subprocess

import pytest

from src.proxy import failover
from src.proxy.failover import FailoverBreaker


def test_a_breaker_built_without_notify_fn_does_not_shell_out(monkeypatch, tmp_path):
    """The default notifier must be suppressible from conftest.

    This is the shape fifteen existing test sites use: no `notify_fn` argument.
    Before the fix the default was bound at def time, so the autouse fixture
    could not reach it and osascript really ran.
    """
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append(a))

    br = FailoverBreaker(
        threshold=1,
        require_offline=False,
        online_fn=lambda: False,
        state_path=tmp_path / "state.json",
    )
    br.record_failure("ConnectTimeout")
    br.record_success()

    assert calls == [], f"a test fired {len(calls)} real notification(s)"


def test_the_autouse_fixture_captures_what_would_have_been_shown(
    _no_desktop_notifications, tmp_path
):
    """The notifications are still observable — suppressed, not deleted."""
    br = FailoverBreaker(
        threshold=1,
        require_offline=False,
        online_fn=lambda: False,
        state_path=tmp_path / "state.json",
    )
    br.record_failure("ConnectTimeout")
    br.record_success()

    messages = [m for _, m in _no_desktop_notifications]
    assert any("routing to local model" in m for m in messages)
    assert any("back to cloud" in m for m in messages)


def test_an_explicit_notify_fn_still_wins(tmp_path):
    seen = []
    br = FailoverBreaker(
        threshold=1,
        require_offline=False,
        online_fn=lambda: False,
        notify_fn=lambda t, m: seen.append(m),
        state_path=tmp_path / "state.json",
    )
    br.record_failure("ConnectTimeout")
    assert seen, "an injected notifier must still be used"
