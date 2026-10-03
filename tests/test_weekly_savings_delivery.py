import importlib.util
import json
import plistlib
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
    rates = DELIVERY.prices(lambda: catalog, model_id="openai/gpt-5.6-sol")
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


def test_baseline_tracks_configured_codex_instead_of_a_stale_model(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('model = "gpt-6.1-sol"\n[profiles.other]\nmodel = "gpt-5.6-sol"\n')
    monkeypatch.delenv("SAVINGS_CODEX_MODEL", raising=False)
    monkeypatch.setenv("SAVINGS_CODEX_CONFIG", str(config))
    assert DELIVERY.codex_baseline_model() == "openai/gpt-6.1-sol"
    monkeypatch.setenv("SAVINGS_CODEX_MODEL", "openai/gpt-6-astra")
    assert DELIVERY.codex_baseline_model() == "openai/gpt-6-astra"


def test_missing_baseline_is_not_silently_replaced_with_old_pricing(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('[profiles.other]\nmodel = "gpt-5.6-sol"\n')
    monkeypatch.delenv("SAVINGS_CODEX_MODEL", raising=False)
    monkeypatch.setenv("SAVINGS_CODEX_CONFIG", str(config))
    with pytest.raises(DELIVERY.DataUnavailable, match="SAVINGS_CODEX_MODEL"):
        DELIVERY.codex_baseline_model()


def test_damaged_jury_ledger_blocks_a_snapshot(tmp_path):
    ledger = tmp_path / "spend.jsonl"
    ledger.write_text('{"ts":"2026-09-25T00:00:00+00:00","cost_usd":"unknown"}\n')
    with pytest.raises(DELIVERY.DataUnavailable, match="ledger is invalid"):
        DELIVERY.validate_jury_ledger(ledger)


def snapshot(cycle, generated):
    rate = {"prompt": 2e-6, "completion": 1e-5, "input_cache_read": 2e-7}
    cloud = {"calls": 1, "input": 1000, "output": 100, "cached": 0, "actual_usd": 0}
    subscription = {"baseline": "same_codex_subscription", "subscription_savings_usd": 0,
                    "current_plan": "ChatGPT Pro 500", "evidence_message_id": "bill-1"}
    return {"schema": 4, "cycle_id": cycle["id"],
            "window_start": cycle["start"].isoformat(), "window_end": cycle["end"].isoformat(),
            "generated_at": generated.astimezone(timezone.utc).isoformat(),
            "comparison": [], "pricing": {"openai/gpt-5.6-sol": rate}, "sources": {},
            "baseline_model": "openai/gpt-5.6-sol", "cloud_codex": cloud,
            "subscription": subscription,
            "subscription_value": DELIVERY.subscription_value(subscription, cloud, [], rate),
            "actual_metered_usd": 0, "net_savings_usd": 0}


def test_comparison_subtracts_spend_and_preserves_negative_savings():
    rate = {"prompt": 2e-6, "completion": 1e-5, "input_cache_read": 2e-7}
    row = DELIVERY.comparison_row("JEV", {"calls": 1, "input": 1000, "cached": 100,
                                          "output": 100, "actual_usd": 0.01}, rate)
    assert row["codex_equivalent_usd"] == pytest.approx(0.00282)
    assert row["net_savings_usd"] == pytest.approx(-0.00718)


def test_provider_ledger_deduplicates_receipts_and_excludes_frontiers_and_end_boundary():
    receipt = {"ts": when(25).isoformat(), "backend": "openrouter", "id": "JEV-1",
               "prompt_tokens": 100, "completion_tokens": 10, "cost_usd": 0.001}
    subscription = {**receipt, "id": "frontier", "billing": "subscription", "avoided_usd": 100}
    boundary = {**receipt, "id": "next-week", "ts": when(27).isoformat()}
    text = "\n".join(json.dumps(row) for row in (receipt, receipt, subscription, boundary))
    usage = DELIVERY.ledger_usage(text, when(20), when(27), deduplicate=True)
    assert usage["openrouter"]["calls"] == 1
    assert usage["openrouter"]["actual_usd"] == pytest.approx(0.001)


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -1])
def test_nonfinite_or_negative_spend_cannot_be_counted_as_savings(invalid):
    text = json.dumps({"ts": when(25).isoformat(), "backend": "openrouter",
                       "prompt_tokens": 10, "completion_tokens": 2, "cost_usd": invalid})
    with pytest.raises(DELIVERY.DataUnavailable):
        DELIVERY.ledger_usage(text, when(20), when(27))


def test_local_jury_and_metered_jury_keep_separate_receipts():
    rows = [{"ts": when(25).isoformat(), "backend": backend,
             "prompt_tokens": 100, "completion_tokens": 10, "cost_usd": cost}
            for backend, cost in (("ollama", 0), ("openrouter", 0.004))]
    usage = DELIVERY.ledger_usage("\n".join(map(json.dumps, rows)), when(20), when(27))
    assert usage["ollama"]["input"] == 100
    assert usage["openrouter"]["actual_usd"] == 0.004


def test_provider_cached_tokens_are_preserved_and_missing_cost_is_not_free():
    row = {"ts": when(25).isoformat(), "backend": "openrouter", "prompt_tokens": 100,
           "completion_tokens": 10, "cached_tokens": 80, "cost_usd": 0.001}
    assert DELIVERY.ledger_usage(json.dumps(row), when(20), when(27))["openrouter"]["cached"] == 80
    for invalid in ({"cached_tokens": 101}, {"cost_available": False}):
        with pytest.raises(DELIVERY.DataUnavailable):
            DELIVERY.ledger_usage(json.dumps({**row, **invalid}), when(20), when(27))


def test_legacy_mismatched_and_naive_snapshots_cannot_send():
    cycle = DELIVERY.cycle_for(when(28))
    data = snapshot(cycle, when(28))
    for changed in ({"schema": 1}, {"schema": 2}, {"schema": 3},
                    {"window_start": when(21).isoformat()},
                    {"generated_at": "2026-09-28T19:07:00"}):
        assert not DELIVERY.snapshot_fresh({**data, **changed}, cycle, when(28))


def test_snapshot_combines_native_receipts_against_one_codex_rate(tmp_path, monkeypatch):
    claude_path = tmp_path / "claude"
    codex_path = tmp_path / "codex"
    claude_path.mkdir()
    codex_path.mkdir()
    qwen_path = tmp_path / "qwen.jsonl"
    qwen_path.write_text("")
    jury_path = tmp_path / "jury.jsonl"
    jury_path.write_text("\n".join(json.dumps(row) for row in [
        {"ts": when(25).isoformat(), "backend": "ollama", "prompt_tokens": 1000,
         "completion_tokens": 100, "cost_usd": 0},
        {"ts": when(25).isoformat(), "backend": "openrouter", "prompt_tokens": 1000,
         "completion_tokens": 100, "cost_usd": 0.001},
        {"ts": when(25).isoformat(), "backend": "codex", "billing": "subscription",
         "prompt_tokens": 1000000, "completion_tokens": 1000000,
         "cost_usd": 0, "avoided_usd": 1000},
    ]))
    monkeypatch.setattr(DELIVERY.report, "PROJECTS_DIR", str(claude_path))
    monkeypatch.setattr(DELIVERY.report, "CODEX_SESSIONS_DIR", str(codex_path))
    monkeypatch.setattr(DELIVERY.report, "LLMJURY_SPEND_LEDGER", str(jury_path))
    monkeypatch.setattr(DELIVERY, "QWEN_USAGE", qwen_path)
    monkeypatch.setattr(DELIVERY.report, "scan", lambda *args, **kwargs: (None, {}, None, 0, None))
    cloud = {"input": 3000, "output": 300, "cache_read": 0, "turns": 1}
    monkeypatch.setattr(DELIVERY.report, "scan_codex", lambda *args, **kwargs:
                        ({"gpt-5.6-sol": cloud}, 1))
    rate = {"prompt": 2e-6, "completion": 1e-5, "input_cache_read": 2e-7}
    jev = {"calls": 1, "input": 1000, "output": 100, "cached": 0, "actual_usd": 0.0001}
    cycle = DELIVERY.cycle_for(when(28))
    result = DELIVERY.build_snapshot(cycle, when(28),
              lambda: {"openai/gpt-5.6-sol": rate, "anthropic/claude-opus-5": rate},
              lambda *args: jev, lambda now: {"current_plan": "ChatGPT Pro 500",
                  "scheduled_plan": "ChatGPT Pro 200", "scheduled_date": "2026-10-30",
                  "evidence_message_id": "bill-1", "evidence_at": when(25).isoformat()})
    assert result["schema"] == 4
    assert result["codex_equivalent_usd"] == pytest.approx(0.009)
    assert result["actual_metered_usd"] == pytest.approx(0.0011)
    assert result["api_equivalent_difference_usd"] == pytest.approx(0.0079)
    assert result["net_savings_usd"] == pytest.approx(-0.0011)
    assert result["subscription"]["subscription_savings_usd"] == 0
    value = result["subscription_value"]
    assert value["offloaded_tokens"] == 3300
    assert value["cloud_tokens"] == 3300
    assert value["offloaded_share"] == pytest.approx(0.5)
    assert value["preserved_value_usd"] == pytest.approx(500 * 12 / 52 * 0.5)
    assert value["net_value_usd"] == pytest.approx(500 * 12 / 52 * 0.5 - 0.0011)
    body = DELIVERY.email_body(result)
    assert "JEV through OpenRouter" in body
    assert "Estimated net Codex subscription value preserved" in body
    assert "Measured offloaded tokens: 3,300" in body
    assert "Subscription bill reduction attributed to offloading: $0.00" in body
    assert "This is pending" in body
    assert "$0.0079" not in body
    assert "earlier calls are unmeasured" in body


def plan_message(text, sender="OpenAI <noreply@tm.openai.com>"):
    return {"sender": sender, "subject": "ChatGPT - Your updated plan",
            "messageId": "bill-1", "messageTimestamp": "2026-09-30T23:14:06Z",
            "messageText": text}


def test_pending_plan_reduction_is_not_a_current_discount():
    message = plan_message("Your ChatGPT Pro 500 subscription will remain active until "
                           "Oct 30, 2026, when your ChatGPT Pro 200 subscription will take effect.")
    current = datetime(2026, 10, 2, tzinfo=PT)
    result = DELIVERY.billing_state_from_messages([message], current)
    assert result["current_plan"] == "ChatGPT Pro 500"
    assert result["scheduled_plan"] == "ChatGPT Pro 200"
    assert result["scheduled_date"] == "2026-10-30"
    with pytest.raises(DELIVERY.DataUnavailable, match="fresh billing confirmation"):
        DELIVERY.billing_state_from_messages([message], datetime(2026, 10, 30, tzinfo=PT))


def test_billing_reads_latest_verified_plan_and_rejects_spoofed_and_future_messages():
    text = "Your subscription has been upgraded from ChatGPT Pro 200 to ChatGPT Pro 500."
    message = plan_message(text)
    now = datetime(2026, 10, 2, tzinfo=PT)
    result = DELIVERY.billing_state_from_messages([message], now)
    assert result["current_plan"] == "ChatGPT Pro 500"
    for invalid in ({**message, "sender": "noreply@tm.openai.com.evil.test"},
                    {**message, "messageTimestamp": "2026-10-20T00:00:00Z"},
                    {**message, "messageId": ""}):
        with pytest.raises(DELIVERY.DataUnavailable):
            DELIVERY.billing_state_from_messages([invalid], now)


def test_forged_positive_subscription_savings_and_nan_cash_cannot_send():
    cycle = DELIVERY.cycle_for(when(28))
    data = snapshot(cycle, when(28))
    invalid = {**data, "subscription": {**data["subscription"], "subscription_savings_usd": 300}}
    assert not DELIVERY.snapshot_fresh(invalid, cycle, when(28))
    for changed in ({"net_savings_usd": 20}, {"actual_metered_usd": float("nan")},
                    {"actual_metered_usd": -1}):
        assert not DELIVERY.snapshot_fresh({**data, **changed}, cycle, when(28))


def test_billing_mailbox_mismatch_stops_before_reading_messages(monkeypatch):
    calls = []
    def execute(command, **kwargs):
        calls.append(command)
        return DELIVERY.subprocess.CompletedProcess(command, 0, json.dumps({
            "successful": True, "data": {"emailAddress": "other@example.com"}}))
    monkeypatch.setattr(DELIVERY.subprocess, "run", execute)
    with pytest.raises(DELIVERY.DataUnavailable, match="identity did not match"):
        DELIVERY.billing_state(datetime(2026, 10, 2, tzinfo=PT))
    assert len(calls) == 1


def test_collector_environment_can_find_the_installed_billing_cli():
    path = SCRIPT.parents[1] / "deploy/com.screddy.weekly-savings-collector.plist"
    with path.open("rb") as stream:
        config = plistlib.load(stream)
    environment = config["EnvironmentVariables"]
    assert environment["HOME"] + "/.composio" in environment["PATH"].split(":")
    assert "/opt/homebrew/bin" in environment["PATH"].split(":")


def test_send_subject_uses_estimated_subscription_value_and_tokens(monkeypatch):
    captured = []
    data = {"cycle_id": "2026-09-27", "actual_metered_usd": 0.04,
            "net_savings_usd": -0.04, "api_equivalent_difference_usd": 400,
            "subscription_value": {"status": "estimated", "net_value_usd": 10.25,
                                   "offloaded_tokens": 3000}}
    monkeypatch.setattr(DELIVERY, "email_body", lambda _: "subscription cash report")
    def execute(command, **kwargs):
        captured.append(json.loads(command[-1]))
        return DELIVERY.subprocess.CompletedProcess(command, 0, json.dumps({
            "successful": True, "data": {"messageId": "gmail-1"}}))
    monkeypatch.setattr(DELIVERY.subprocess, "run", execute)
    DELIVERY.send(data)
    assert captured[0]["subject"].startswith("$10.2500 estimated Codex subscription value preserved; "
                                            "3,000 tokens offloaded")
    assert "$400" not in captured[0]["subject"]


def test_jev_read_failure_defers_instead_of_inventing_zero_spend(tmp_path, monkeypatch):
    local = tmp_path / "jev.jsonl"
    local.write_text("")
    monkeypatch.setattr(DELIVERY, "JEV_USAGE", local)
    def failed(*args, **kwargs):
        raise DELIVERY.subprocess.CalledProcessError(1, "ssh")
    monkeypatch.setattr(DELIVERY.subprocess, "run", failed)
    with pytest.raises(DELIVERY.DataUnavailable, match="JEV receipt collection failed"):
        DELIVERY.jev_usage(when(20), when(27))


def test_jev_combines_desktop_and_remote_receipts_without_duplicates(tmp_path, monkeypatch):
    row = {"id": "jev-1", "ts": when(25).isoformat(), "backend": "openrouter",
           "prompt_tokens": 1000, "completion_tokens": 100, "cost_usd": 0.001}
    local = tmp_path / "jev.jsonl"
    local.write_text(json.dumps(row))
    monkeypatch.setattr(DELIVERY, "JEV_USAGE", local)
    remote = json.dumps(row) + "\n" + json.dumps({**row, "id": "jev-2"})
    monkeypatch.setattr(DELIVERY.subprocess, "run", lambda *args, **kwargs:
                        DELIVERY.subprocess.CompletedProcess("ssh", 0, remote))
    result = DELIVERY.jev_usage(when(20), when(27))
    assert result["calls"] == 2
    assert result["input"] + result["output"] == 2200
    assert result["actual_usd"] == pytest.approx(0.002)
    monkeypatch.setattr(DELIVERY, "JEV_USAGE", tmp_path / "missing.jsonl")
    with pytest.raises(DELIVERY.DataUnavailable, match="JEV receipt collection failed"):
        DELIVERY.jev_usage(when(20), when(27))


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


def value_usage(input_tokens=1000, output_tokens=100, cached=0, spend=0):
    return {"label": "Local checks", "calls": 1, "input": input_tokens,
            "output": output_tokens, "cached": cached, "actual_usd": spend}


def test_subscription_allocation_uses_consistent_input_output_cache_weights():
    rate = {"prompt": 2e-6, "completion": 1e-5, "input_cache_read": 2e-7}
    local = value_usage(cached=500, spend=0.04)
    result = DELIVERY.subscription_value({"current_plan": "ChatGPT Pro 200"},
                                         value_usage(), [local], rate)
    share = 0.0021 / (0.003 + 0.0021)
    assert result["offloaded_tokens"] == 1100
    assert result["offloaded_share"] == pytest.approx(share)
    assert result["net_value_usd"] == pytest.approx(200 * 12 / 52 * share - 0.04)
    assert "not an invoice" in result["budget_basis"]


def test_no_offloading_preserves_no_value_and_expensive_checks_keep_negative_value():
    rate = {"prompt": 2e-6, "completion": 1e-5, "input_cache_read": 2e-7}
    plan = {"current_plan": "ChatGPT Pro 500"}
    empty = DELIVERY.subscription_value(plan, value_usage(), [], rate)
    assert empty["preserved_value_usd"] == 0
    assert empty["net_value_usd"] == 0
    expensive = DELIVERY.subscription_value(plan, value_usage(), [value_usage(spend=100)], rate)
    assert expensive["preserved_value_usd"] == pytest.approx(500 * 12 / 52 * 0.5)
    assert expensive["net_value_usd"] < 0
    assert expensive["preserved_value_usd"] <= expensive["weekly_nominal_usd"]


@pytest.mark.parametrize("offloads", [[], [value_usage()]])
def test_missing_codex_usage_is_not_a_hundred_percent_saving(offloads):
    rate = {"prompt": 2e-6, "completion": 1e-5, "input_cache_read": 2e-7}
    result = DELIVERY.subscription_value({"current_plan": "ChatGPT Pro 500"},
                                         DELIVERY.usage_bucket(), offloads, rate)
    assert result["status"] == "unavailable_without_cloud_codex_usage"
    assert result["offloaded_share"] is None
    assert result["preserved_value_usd"] is None
    assert result["net_value_usd"] is None


@pytest.mark.parametrize("changed", [{"input": -1}, {"input": 1.5}, {"cached": 1001},
                                    {"output": True}, {"actual_usd": float("nan")}])
def test_invalid_native_tokens_or_cost_cannot_create_plan_value(changed):
    rate = {"prompt": 2e-6, "completion": 1e-5, "input_cache_read": 2e-7}
    with pytest.raises(DELIVERY.DataUnavailable):
        DELIVERY.subscription_value({"current_plan": "ChatGPT Pro 500"}, value_usage(),
                                     [{**value_usage(), **changed}], rate)


def test_unsupported_plan_and_forged_value_cannot_send():
    cycle = DELIVERY.cycle_for(when(28))
    data = snapshot(cycle, when(28))
    forged = {**data["subscription_value"], "net_value_usd": 1000}
    assert not DELIVERY.snapshot_fresh({**data, "subscription_value": forged}, cycle, when(28))
    unsupported = {**data["subscription"], "current_plan": "Unknown plan"}
    assert not DELIVERY.snapshot_fresh({**data, "subscription": unsupported}, cycle, when(28))


@pytest.mark.parametrize("changed", [{"prompt": 0}, {"completion": -1},
                                    {"input_cache_read": float("nan")},
                                    {"input_cache_read": 1e-5}])
def test_invalid_weighting_cannot_overstate_offload_share(changed):
    rate = {"prompt": 2e-6, "completion": 1e-5, "input_cache_read": 2e-7, **changed}
    with pytest.raises(DELIVERY.DataUnavailable):
        DELIVERY.subscription_value({"current_plan": "ChatGPT Pro 500"}, value_usage(),
                                     [value_usage()], rate)
