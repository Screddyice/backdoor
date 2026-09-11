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


def test_openrouter_usage_is_identified_without_mixing_in_local_models():
    usage = {
        "anthropic/claude-sonnet-4": {"turns": 2},
        "openai/gpt-5": {"turns": 3},
        "Qwen": {"turns": 5},
        "claude-opus-5": {"turns": 7},
    }

    routed = REPORT.openrouter_usage(usage)

    assert set(routed) == {"anthropic/claude-sonnet-4", "openai/gpt-5"}


def test_email_keeps_local_savings_separate_from_claude_and_codex_openrouter_usage():
    body = REPORT.build_savings_email_md(
        {
            "usd_saved": 40.0,
            "cache_rate": 50.0,
            "local_saved": 10.0,
            "local_turns": 2,
            "local_claude_turns": 2,
            "local_claude_tokens": 900_000,
            "local_codex_turns": 1,
            "local_codex_tokens": 300_000,
            "openrouter_claude_turns": 3,
            "openrouter_claude_tokens": 1_200_000,
            "openrouter_codex_turns": 5,
            "openrouter_codex_tokens": 2_500_000,
            "codex_saved": 30.0,
            "codex_turns": 4,
        },
        "2026-08-27",
        "2026-09-03",
    )

    assert "Open-source models (local) | $10.00" in body
    assert "Codex plan | $30.00" in body
    assert "Local models via Claude | 2 | 900K" in body
    assert "Local models via Codex | 1 | 300K" in body
    assert "OpenRouter via Claude | 3 | 1.2M" in body
    assert "OpenRouter via Codex | 5 | 2.5M" in body
    assert "**Total: $40.00 saved.**" in body


def test_email_reports_zero_when_no_transcript_attributed_openrouter_usage():
    body = REPORT.build_savings_email_md(
        {
            "usd_saved": 0.0,
            "cache_rate": 0.0,
            "local_saved": 0.0,
            "local_turns": 0,
            "local_claude_turns": 12,
            "local_claude_tokens": 4_000_000,
            "local_codex_turns": 8,
            "local_codex_tokens": 2_000_000,
            "openrouter_claude_turns": 0,
            "openrouter_claude_tokens": 0,
            "openrouter_codex_turns": 0,
            "openrouter_codex_tokens": 0,
            "codex_saved": 0.0,
            "codex_turns": 0,
        },
        "2026-08-27",
        "2026-09-03",
    )

    assert "Local models via Claude | 12 | 4.0M" in body
    assert "Local models via Codex | 8 | 2.0M" in body
    assert "Measured account spend" not in body
    assert "Attribution unavailable" not in body
    assert "OpenRouter via Claude | 0 | 0" in body
    assert "OpenRouter via Codex | 0 | 0" in body


# --- local attribution: aliased local tags must not be priced as cloud -------


def test_is_local_recognises_the_catalog_aliases_the_qwen_launcher_uses():
    """`qwen claude` ships local tags under claude-* catalog ids.

    Claude Code validates the session model against its compiled catalog, so the
    launcher registers the local weights under a catalog name (`ollama cp`).
    A prefix test on "qwen" misses those, and the turn is then priced with the
    unknown-claude fallback — opus-tier — inventing cloud spend for a turn that
    cost nothing and understating the saving it actually produced.
    """
    assert REPORT.is_local("claude-qwen-27b")
    assert REPORT.is_local("claude-qwen38-obliterated")
    # Case is not meaningful in a model id a person types.
    assert REPORT.is_local("Claude-Qwen-27B")


def test_is_local_still_rejects_real_cloud_models():
    """The alias rule must not swallow first-party models."""
    for model in (
        "claude-opus-5",
        "claude-sonnet-5",
        "claude-haiku-4-5-20251001",
        "claude-fable-5-1",
    ):
        assert not REPORT.is_local(model), model


def test_local_prefix_matching_is_unchanged_for_bare_tags():
    for model in ("qwen", "Qwen", "qwen3.8:27b-obliterated", "gemma3:12b", "phi4-mini:3.8b"):
        assert REPORT.is_local(model), model


# --- send resilience: a transient outage must not cost a week's report -------


class _Run:
    """Records composio invocations and replays a scripted result sequence."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        outcome = self.results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Completed:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


_OK = _Completed(stdout=json.dumps({"successful": True}))
# The real failure that stopped the weekly mail: DNS lookup failed mid-send.
_DNS = _Completed(stdout="", stderr="Caused by: getaddrinfo ENOTFOUND backend.composio.dev",
                  returncode=1)


def _savings():
    return {"usd_saved": 1234.0}


def test_send_retries_a_transient_network_failure_and_succeeds(monkeypatch, tmp_path):
    """A DNS blip at send time must not cost the whole week's report.

    The job fires once a week. Before this, one `getaddrinfo ENOTFOUND` meant
    the report was written to disk and never mailed, and the only trace was a
    .err file nobody reads.
    """
    env = tmp_path / ".env"
    env.write_text("TMN_COMPOSIO_API_KEY=abc123\n")
    monkeypatch.setattr(REPORT, "ENV_FILE", str(env))
    monkeypatch.setattr(REPORT, "md_to_html", lambda md: "<p>x</p>")
    monkeypatch.setattr(REPORT, "build_savings_email_md", lambda *a: "x")
    monkeypatch.setattr(REPORT.time, "sleep", lambda _s: None)
    run = _Run([_DNS, _DNS, _OK])
    monkeypatch.setattr(REPORT.subprocess, "run", run)

    assert REPORT.send_weekly_email(_savings(), "2026-09-04", "2026-09-11", dry=False)
    assert run.calls == 3


def test_send_gives_up_after_the_retry_budget_and_reports_failure(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("TMN_COMPOSIO_API_KEY=abc123\n")
    monkeypatch.setattr(REPORT, "ENV_FILE", str(env))
    monkeypatch.setattr(REPORT, "md_to_html", lambda md: "<p>x</p>")
    monkeypatch.setattr(REPORT, "build_savings_email_md", lambda *a: "x")
    monkeypatch.setattr(REPORT.time, "sleep", lambda _s: None)
    run = _Run([_DNS] * REPORT.SEND_ATTEMPTS)
    monkeypatch.setattr(REPORT.subprocess, "run", run)

    assert not REPORT.send_weekly_email(_savings(), "2026-09-04", "2026-09-11", dry=False)
    assert run.calls == REPORT.SEND_ATTEMPTS


def test_send_does_not_retry_a_rejection_the_server_actually_answered(monkeypatch, tmp_path):
    """A delivered verdict is not a transport problem — retrying just repeats it."""
    env = tmp_path / ".env"
    env.write_text("TMN_COMPOSIO_API_KEY=abc123\n")
    monkeypatch.setattr(REPORT, "ENV_FILE", str(env))
    monkeypatch.setattr(REPORT, "md_to_html", lambda md: "<p>x</p>")
    monkeypatch.setattr(REPORT, "build_savings_email_md", lambda *a: "x")
    monkeypatch.setattr(REPORT.time, "sleep", lambda _s: None)
    rejected = _Completed(stdout=json.dumps({"successful": False, "error": "invalid recipient"}))
    run = _Run([rejected])
    monkeypatch.setattr(REPORT.subprocess, "run", run)

    assert not REPORT.send_weekly_email(_savings(), "2026-09-04", "2026-09-11", dry=False)
    assert run.calls == 1


# --- llm-jury spend: read its ledger, never its key --------------------------


def test_llmjury_spend_sums_only_records_inside_the_window(tmp_path, monkeypatch):
    """llm-jury's OpenRouter spend reaches the report as an artifact it writes.

    Backdoor deliberately does not hold OPENROUTER_API_KEY (removed 2026-08-26):
    it is another system's credential, and an account-wide total cannot be
    attributed to a client anyway. A ledger llm-jury writes is both precise and
    key-free.
    """
    ledger = tmp_path / "spend.jsonl"
    ledger.write_text("".join(json.dumps(r) + "\n" for r in [
        {"ts": "2026-09-10T12:00:00+00:00", "backend": "openrouter",
         "model": "deepseek/deepseek-v4-flash", "cost_usd": 0.25},
        {"ts": "2026-09-09T12:00:00+00:00", "backend": "openrouter",
         "model": "anthropic/claude-opus-5", "cost_usd": 1.00},
        # Outside a 7-day window ending 2026-09-11.
        {"ts": "2026-08-01T12:00:00+00:00", "backend": "openrouter",
         "model": "deepseek/deepseek-v4-pro", "cost_usd": 99.0},
    ]))
    monkeypatch.setattr(REPORT, "LLMJURY_SPEND_LEDGER", str(ledger))
    now = datetime(2026, 9, 11, tzinfo=timezone.utc)

    spend = REPORT.llmjury_spend(days=7, now=now)
    assert spend["available"] is True
    assert spend["usd"] == pytest.approx(1.25)
    assert spend["calls"] == 2


def test_llmjury_spend_is_unavailable_not_zero_when_the_ledger_is_missing(tmp_path, monkeypatch):
    """Absent must not read as free. Zero and unknown are different claims."""
    monkeypatch.setattr(REPORT, "LLMJURY_SPEND_LEDGER", str(tmp_path / "nope.jsonl"))
    spend = REPORT.llmjury_spend(days=7, now=datetime(2026, 9, 11, tzinfo=timezone.utc))
    assert spend["available"] is False
    assert spend["usd"] == 0.0


def test_llmjury_spend_skips_corrupt_lines_without_losing_the_rest(tmp_path, monkeypatch):
    ledger = tmp_path / "spend.jsonl"
    ledger.write_text(
        '{"ts": "2026-09-10T12:00:00+00:00", "cost_usd": 0.50}\n'
        'not json at all\n'
        '{"ts": "no-such-date", "cost_usd": 5.0}\n'
        '{"ts": "2026-09-10T13:00:00+00:00", "cost_usd": 0.25}\n'
    )
    monkeypatch.setattr(REPORT, "LLMJURY_SPEND_LEDGER", str(ledger))
    spend = REPORT.llmjury_spend(days=7, now=datetime(2026, 9, 11, tzinfo=timezone.utc))
    assert spend["usd"] == pytest.approx(0.75)
    assert spend["calls"] == 2
