"""Safe defaults for offline context virtualization."""

from src.proxy.config import Settings


def test_context_virtualization_defaults_are_safe():
    settings = Settings(_env_file=None)

    assert settings.context_virtualization is False
    assert settings.context_store_path == "~/.backdoor/context/transcripts.sqlite3"
    assert settings.context_store_max_bytes == 1_073_741_824
    assert settings.context_inactive_days == 30
    assert settings.context_target_input_tokens == 18_000
    assert settings.context_hard_input_tokens == 22_000
    assert settings.context_retrieval_tokens == 5_000
    assert settings.context_internal_result_tokens == 2_000
    assert settings.failover_max_output_tokens == 1_024
    assert settings.failover_read_only is True
    assert settings.failover_first_text_seconds == 30.0
    assert settings.failover_total_seconds == 60.0
    assert settings.context_archive_queue_size == 32
    assert settings.context_archive_timeout_seconds == 0.5
    assert settings.context_assembly_timeout_seconds == 2.5
    assert settings.context_response_cache_seconds == 600
    assert settings.failover_recovery_successes == 2

