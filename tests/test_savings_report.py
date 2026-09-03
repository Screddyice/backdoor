import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "claude-savings-report.py"
SPEC = importlib.util.spec_from_file_location("savings_report", SCRIPT)
REPORT = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(REPORT)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def test_scan_codex_counts_each_usage_snapshot_once_and_keeps_models_separate(tmp_path):
    total_one = {
        "input_tokens": 120,
        "cached_input_tokens": 20,
        "output_tokens": 30,
        "reasoning_output_tokens": 5,
        "total_tokens": 150,
    }
    _write_jsonl(
        tmp_path / "2026" / "09" / "03" / "session.jsonl",
        [
            {
                "type": "turn_context",
                "timestamp": "2026-09-03T01:00:00Z",
                "payload": {"type": "turn_context", "model": "gpt-5.6-sol"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-09-03T01:00:01Z",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": total_one,
                        "last_token_usage": total_one,
                    },
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-09-03T01:00:02Z",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": total_one,
                        "last_token_usage": total_one,
                    },
                },
            },
            {
                "type": "turn_context",
                "timestamp": "2026-09-03T01:05:00Z",
                "payload": {"type": "turn_context", "model": "qwen"},
            },
            {
                "type": "event_msg",
                "timestamp": "2026-09-03T01:05:01Z",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 160,
                            "cached_input_tokens": 20,
                            "output_tokens": 40,
                            "reasoning_output_tokens": 5,
                            "total_tokens": 200,
                        },
                        "last_token_usage": {
                            "input_tokens": 40,
                            "cached_input_tokens": 0,
                            "output_tokens": 10,
                            "reasoning_output_tokens": 0,
                            "total_tokens": 50,
                        },
                    },
                },
            },
        ],
    )

    usage, scanned = REPORT.scan_codex(
        datetime(2026, 9, 1, tzinfo=timezone.utc), sessions_dir=str(tmp_path)
    )

    assert scanned == 1
    assert usage["gpt-5.6-sol"] == {
        "model": "gpt-5.6-sol",
        "input": 120,
        "output": 30,
        "cache_read": 20,
        "cache_w5m": 0,
        "cache_w1h": 0,
        "turns": 1,
    }
    assert usage["qwen"]["input"] == 40
    assert usage["qwen"]["output"] == 10
    assert usage["qwen"]["turns"] == 1


def test_codex_plan_savings_uses_cached_rate_and_subtracts_the_plan():
    usage = {
        "gpt-5.6-sol": {
            "model": "gpt-5.6-sol",
            "input": 1_000_000,
            "output": 100_000,
            "cache_read": 800_000,
            "cache_w5m": 0,
            "cache_w1h": 0,
            "turns": 4,
        }
    }

    value, saved = REPORT.codex_plan_savings(
        usage,
        input_per_mtok=2.5,
        cached_input_per_mtok=0.25,
        output_per_mtok=15.0,
        weekly_plan_cost=1.0,
    )

    assert value == pytest.approx(2.2)
    assert saved == pytest.approx(1.2)


def test_openrouter_savings_nets_measured_spend_from_counterfactual_value():
    value, saved = REPORT.openrouter_savings(3.0, cost_ratio=10.0)

    assert value == 30.0
    assert saved == 27.0


def test_email_lists_open_source_openrouter_and_codex_savings():
    body = REPORT.build_savings_email_md(
        {
            "usd_saved": 60.0,
            "cache_rate": 50.0,
            "local_saved": 10.0,
            "local_turns": 2,
            "openrouter_saved": 20.0,
            "openrouter_spend": 1.0,
            "openrouter_available": True,
            "codex_saved": 30.0,
            "codex_turns": 4,
        },
        "2026-08-27",
        "2026-09-03",
    )

    assert "Open-source models (local) | $10.00" in body
    assert "OpenRouter | $20.00" in body
    assert "Codex plan | $30.00" in body
    assert "**Total: $60.00 saved.**" in body


def test_email_marks_openrouter_unavailable_instead_of_claiming_zero_spend():
    body = REPORT.build_savings_email_md(
        {
            "usd_saved": 0.0,
            "cache_rate": 0.0,
            "local_saved": 0.0,
            "local_turns": 0,
            "openrouter_saved": 0.0,
            "openrouter_spend": 0.0,
            "openrouter_available": False,
            "codex_saved": 0.0,
            "codex_turns": 0,
        },
        "2026-08-27",
        "2026-09-03",
    )

    assert "OpenRouter | $0.00 | Usage unavailable; excluded from the total" in body
