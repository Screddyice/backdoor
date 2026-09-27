import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/weekly-savings-delivery.py"
SPEC = importlib.util.spec_from_file_location("weekly_savings_delivery", SCRIPT)
DELIVERY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DELIVERY)
PT = ZoneInfo("America/Los_Angeles")


def when(day, hour=19, minute=7):
    return datetime(2026, 9, day, hour, minute, tzinfo=PT)


def test_cycle_closes_sunday_and_allows_three_days_after_monday_send():
    assert DELIVERY.cycle_for(when(27, 19, 6)) is None
    cycle = DELIVERY.cycle_for(when(27, 19, 8))
    assert cycle["start"] == when(20)
    assert cycle["end"] == when(27)
    assert cycle["nominal"] == when(28)
    assert cycle["deadline"] == datetime(2026, 10, 1, 19, 7, tzinfo=PT)
    assert DELIVERY.cycle_for(datetime(2026, 10, 1, 19, 8, tzinfo=PT)) is None


def test_published_prices_supply_the_counterfactual():
    catalog = {"data": [
        {"id": "openai/gpt-5.6-sol", "pricing":
         {"prompt": "0.000002", "completion": "0.000010", "input_cache_read": "0.0000002"}},
        {"id": "anthropic/claude-opus-5", "pricing":
         {"prompt": "0.000005", "completion": "0.000025", "input_cache_read": "0.0000005"}},
    ]}
    rates = DELIVERY.prices(lambda: catalog)
    usage = {"local": {"input": 1_000_000, "cache_read": 200_000,
                       "output": 100_000, "cache_w5m": 0, "cache_w1h": 0}}
    assert DELIVERY.codex_equivalent(usage, rates["openai/gpt-5.6-sol"]) == pytest.approx(2.64)
    assert DELIVERY.claude_equivalent(usage, rates["anthropic/claude-opus-5"]) == pytest.approx(7.6)


def test_qwen_sessions_are_measured_once_and_not_assigned_to_a_client(tmp_path):
    path = tmp_path / "usage.jsonl"
    started = when(25).timestamp() * 1000
    ended = (when(25) + timedelta(minutes=5)).timestamp() * 1000
    path.write_text(json.dumps({"sessionId": "s1", "startTime": started, "timestamp": ended,
                                "models": {"qwen": {"requests": 2, "inputTokens": 1000,
                                                     "cachedTokens": 100, "outputTokens": 50},
                                           "openai/gpt-5.6-sol": {"requests": 1, "inputTokens": 9000,
                                                                  "outputTokens": 500}}}) + "\n")
    usage = DELIVERY.qwen_usage(when(20), when(27), path)
    assert usage == {"sessions": 1, "requests": 2, "input": 1000,
                     "cached": 100, "output": 50}
    with pytest.raises(DELIVERY.DataUnavailable, match="crosses"):
        DELIVERY.qwen_usage(when(25, 19, 8), when(27), path)


def test_damaged_jury_ledger_blocks_a_snapshot(tmp_path):
    ledger = tmp_path / "spend.jsonl"
    ledger.write_text('{"ts":"2026-09-25T00:00:00+00:00","cost_usd":"unknown"}\n')
    with pytest.raises(DELIVERY.DataUnavailable, match="ledger is invalid"):
        DELIVERY.validate_jury_ledger(ledger)


def snapshot(cycle, generated):
    return {"schema": 1, "cycle_id": cycle["id"],
            "generated_at": generated.astimezone(timezone.utc).isoformat(),
            "local": {}, "pricing": {}, "sources": {}}


def test_snapshot_must_cover_the_complete_week_and_be_under_24_hours_old():
    cycle = DELIVERY.cycle_for(when(28))
    assert DELIVERY.snapshot_fresh(snapshot(cycle, when(27, 19, 8)), cycle, when(28))
    assert not DELIVERY.snapshot_fresh(snapshot(cycle, when(27, 19, 6)), cycle, when(28))
    assert not DELIVERY.snapshot_fresh(snapshot(cycle, when(27, 19, 8)), cycle,
                                       when(28, 19, 9))


def test_dispatch_waits_for_snapshot_then_sends_once(tmp_path, monkeypatch):
    monkeypatch.setattr(DELIVERY, "STATE_DIR", tmp_path)
    now = when(28)
    cycle = DELIVERY.cycle_for(now)
    sent = []
    sender = lambda data: sent.append(data) or {"data": {"messageId": "gmail-1"}}
    assert "deferred" in DELIVERY.dispatch(now, sender)
    DELIVERY.atomic_json(DELIVERY.snapshot_path(cycle), snapshot(cycle, now - timedelta(hours=1)))
    assert "sent" in DELIVERY.dispatch(now, sender)
    assert len(sent) == 1
    assert "already sent" in DELIVERY.dispatch(now + timedelta(hours=1), sender)
    assert len(sent) == 1
    assert DELIVERY.read_json(tmp_path / f"delivery-{cycle['id']}.json")["receipt"] == "gmail-1"


def test_uncertain_send_is_never_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(DELIVERY, "STATE_DIR", tmp_path)
    now = when(28)
    cycle = DELIVERY.cycle_for(now)
    DELIVERY.atomic_json(DELIVERY.snapshot_path(cycle), snapshot(cycle, now))
    def uncertain(_):
        raise TimeoutError("Gmail may have accepted it")
    with pytest.raises(TimeoutError):
        DELIVERY.dispatch(now, uncertain)
    assert "already attempted" in DELIVERY.dispatch(now + timedelta(hours=1), uncertain)


def test_expired_week_is_marked_skipped_without_sending(tmp_path, monkeypatch):
    monkeypatch.setattr(DELIVERY, "STATE_DIR", tmp_path)
    now = datetime(2026, 10, 1, 19, 8, tzinfo=PT)
    cycle = DELIVERY.cycle_for(now, include_expired=True)
    assert "skipped" in DELIVERY.dispatch(now, lambda _: pytest.fail("sent after deadline"))
    assert DELIVERY.read_json(tmp_path / f"delivery-{cycle['id']}.json")["status"] == "skipped"


def test_collector_publishes_a_local_record_before_the_send_window(tmp_path, monkeypatch):
    monkeypatch.setattr(DELIVERY, "STATE_DIR", tmp_path)
    published = []
    monkeypatch.setattr(DELIVERY, "build_snapshot", lambda cycle, now: snapshot(cycle, now))
    monkeypatch.setattr(DELIVERY, "publish_snapshot", lambda cycle: published.append(cycle["id"]))
    assert "saved" in DELIVERY.collect(when(27, 20))
    assert published == ["2026-09-27"]
    assert DELIVERY.snapshot_path(DELIVERY.cycle_for(when(27, 20))).exists()


def test_corrupt_delivery_state_cannot_be_treated_as_unsent(tmp_path, monkeypatch):
    monkeypatch.setattr(DELIVERY, "STATE_DIR", tmp_path)
    now = when(28)
    cycle = DELIVERY.cycle_for(now)
    (tmp_path / f"delivery-{cycle['id']}.json").write_text("{broken")
    with pytest.raises(DELIVERY.DataUnavailable, match="unreadable"):
        DELIVERY.dispatch(now, lambda _: pytest.fail("sent with corrupt state"))
