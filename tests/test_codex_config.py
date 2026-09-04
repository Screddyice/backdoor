from pathlib import Path

import pytest
from pydantic import ValidationError

from src.proxy.codex_routes import _failover_statuses
from src.proxy.config import Settings


def test_readme_keeps_codex_on_the_selected_cloud_models_catalog_window():
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    assert 'model_context_window = 32000' not in readme
    assert 'model_auto_compact_token_limit = 27904' not in readme
    assert "uses the selected cloud model's catalog limits" in readme


def test_codex_runtime_settings_accept_operator_environment(monkeypatch):
    monkeypatch.setenv("CODEX_CHATGPT_UPSTREAM", "https://cloud.test/backend-api/codex")
    monkeypatch.setenv("CODEX_LOCAL_RESPONSES_URL", "http://127.0.0.1:11434/v1/responses")
    monkeypatch.setenv("CODEX_LOCAL_MODEL", "qwen3.8:27b-obliterated")
    monkeypatch.setenv("MEMORY_WORKER_URL", "http://127.0.0.1:37701")
    monkeypatch.setenv("CODEX_CONTEXT_WINDOW", "32000")
    monkeypatch.setenv("CODEX_FAILOVER_THRESHOLD", "3")
    monkeypatch.setenv("CODEX_FAILOVER_STATUSES", "429,503,401")
    monkeypatch.setenv("CODEX_MAX_REQUEST_BYTES", "67108864")

    settings = Settings(_env_file=None)

    assert settings.codex_chatgpt_upstream == "https://cloud.test/backend-api/codex"
    assert settings.codex_local_responses_url == "http://127.0.0.1:11434/v1/responses"
    assert settings.codex_local_model == "qwen3.8:27b-obliterated"
    assert settings.memory_worker_url == "http://127.0.0.1:37701"
    assert settings.codex_context_window == 32_000
    assert settings.codex_failover_threshold == 3
    assert settings.codex_max_request_bytes == 64 * 1024 * 1024
    assert _failover_statuses(settings) == {429, 503}


def test_default_codex_allocation_fills_one_32k_window():
    settings = Settings(_env_file=None)
    allocated = (
        settings.codex_system_budget_tokens
        + settings.codex_memory_budget_tokens
        + settings.codex_tools_budget_tokens
        + settings.codex_active_turn_budget_tokens
        + settings.codex_reply_reserve_tokens
    )

    assert allocated == settings.codex_context_window == 32_000


def test_codex_allocation_above_window_is_rejected():
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            codex_context_window=32_000,
            codex_active_turn_budget_tokens=21_001,
        )
