"""Safe defaults and validation for automatic Qwen context compaction."""

import pytest
from pydantic import ValidationError

from src.proxy.config import Settings


def test_context_virtualization_defaults_are_safe():
    settings = Settings(_env_file=None)

    assert settings.context_virtualization is False
    assert settings.context_target_input_tokens == 18_000
    assert settings.context_hard_input_tokens == 22_000
    assert settings.context_archive_path == "~/.backdoor/context/transcripts.sqlite3"
    assert settings.context_archive_max_bytes == 1_073_741_824
    assert settings.context_archive_inactive_days == 30
    assert settings.context_response_cache_seconds == 600
    assert settings.context_archive_timeout_seconds == 0.5
    assert settings.context_assembly_timeout_seconds == 2.5
    assert settings.context_tokenizer_executable == "/opt/homebrew/bin/llama-tokenize"
    assert settings.context_tokenizer_model_path == ""


def test_context_target_cannot_exceed_hard_limit():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, context_target_input_tokens=22_001, context_hard_input_tokens=22_000)


@pytest.mark.parametrize(
    "field,value",
    [
        ("context_target_input_tokens", 0),
        ("context_hard_input_tokens", 0),
        ("context_archive_max_bytes", 1_048_575),
        ("context_archive_inactive_days", 0),
        ("context_archive_timeout_seconds", 0),
        ("context_assembly_timeout_seconds", 0),
    ],
)
def test_context_limits_are_bounded(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
