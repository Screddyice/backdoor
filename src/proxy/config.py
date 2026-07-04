import os
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Server
    host: str = "127.0.0.1"
    port: int = 8082
    log_file: str = "proxy.log"

    # Provider — any OpenAI-compatible API (NIM, DeepSeek, Groq, OpenRouter, Ollama, …)
    provider_api_key: str = ""
    provider_model: str = "meta/llama-3.3-70b-instruct"
    provider_base_url: str = "https://integrate.api.nvidia.com/v1"
    provider_max_tokens: int = 32768
    provider_temperature: float = 1.0
    provider_top_p: float = 1.0
    provider_reasoning_effort: str = ""

    # Router mode:
    #   "profile" (default) — translate EVERY request to the active profile's
    #     OpenAI-compatible backend (classic backdoor behaviour; what the
    #     `qwen` wrapper instance on :8082 uses).
    #   "hybrid" — route by requested model name: names in MODEL_ROUTES go to
    #     their local profile; everything else passes through byte-faithful to
    #     the real Anthropic API. Lets a normal cloud Claude Code session
    #     switch to the local model with `/model qwen`.
    router_mode: str = "profile"
    anthropic_upstream: str = "https://api.anthropic.com"

    # Cloud→local failover (hybrid mode only): when the real Anthropic API is
    # unreachable / usage-limited / overloaded for `failover_threshold`
    # consecutive requests within `failover_window_seconds`, serve passthrough
    # /v1/messages traffic from a local profile instead of failing, probing
    # upstream every `failover_probe_seconds` until it recovers.
    #
    # Which local profile is picked by the session's estimated input tokens
    # (see FAILOVER_LADDER): a small session stays on the fast 4B, a big one
    # escalates to a 9B tier whose context window actually fits it. This is the
    # "pick the appropriate local model for the use case" behavior — for
    # failover the use-case dimension that matters is context size.
    failover_to_local: bool = True
    failover_profile: str = "local-qwen35"  # floor / smallest tier
    failover_threshold: int = 3
    failover_window_seconds: float = 120.0
    failover_probe_seconds: float = 60.0

    # Request optimizations — avoid burning provider quota on Claude Code housekeeping calls
    skip_quota_probes: bool = True
    skip_title_generation: bool = True
    skip_suggestion_mode: bool = True
    mock_prefix_detection: bool = True
    mock_filepath_extraction: bool = True

    # Telegram (optional)
    telegram_bot_token: str = ""
    telegram_allowed_user_id: int | None = None

    # CLI sessions (used by Telegram integration)
    claude_workspace: str = "./workspace"
    max_cli_sessions: int = 5


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache():
    get_settings.cache_clear()


# Model names a Claude Code session can request (via `/model <name>` or
# `--model <name>`) that route to a LOCAL profile in hybrid mode.
MODEL_ROUTES: dict[str, str] = {
    "qwen": "local-qwen35",
    "qwen-fast": "local-fast",
}


# Cloud→local failover ladder: pick the local profile whose context window fits
# the failed-over session. Each entry is (max_input_tokens_inclusive, profile);
# the first entry whose bound is >= the estimated input token count wins, else
# the last. Bounds sit below each tag's real window to leave room for the reply.
# Measured load at OLLAMA_NUM_PARALLEL=4 (q8_0 KV + flash attention): 4b-64k
# ~7GB, 9b-128k ~12GB, 9b-256k ~16GB — all fit 36GB individually, and Ollama
# evicts idle models to make room. The 9B tiers deliberately break the
# "harness = 4B" rule: during an outage, a big session kept ALIVE on a 9B beats
# one truncated to fit a 4B.
FAILOVER_LADDER: list[tuple[float, str]] = [
    (52_000, "local-qwen35"),          # qwen3.5:4b-64k   (~65K window)
    (115_000, "local-failover-128k"),  # qwen3.5:9b-128k  (~131K window)
    (float("inf"), "local-failover-256k"),  # qwen3.5:9b-256k (~262K window)
]


def pick_failover_profile(est_input_tokens: int) -> str:
    """Choose the failover profile whose window fits this session's size."""
    for bound, profile in FAILOVER_LADDER:
        if est_input_tokens <= bound:
            return profile
    return FAILOVER_LADDER[-1][1]


@lru_cache(maxsize=8)
def load_profile_settings(profile: str) -> Settings:
    """Settings built from profiles/<profile>.env (relative to the repo cwd).

    Process env still takes precedence in pydantic-settings, so the router
    instance must not export PROVIDER_* vars (the LaunchAgent doesn't)."""
    path = f"profiles/{profile}.env"
    if not os.path.exists(path):
        raise FileNotFoundError(f"profile env not found: {path}")
    return Settings(_env_file=path)
