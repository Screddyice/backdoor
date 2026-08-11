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

    # Cloud→local failover (hybrid mode only): when THIS HOST IS OFFLINE —
    # `failover_threshold` consecutive transport errors within
    # `failover_window_seconds`, confirmed by a connectivity probe — serve
    # passthrough /v1/messages traffic from a local profile instead of failing,
    # probing upstream every `failover_probe_seconds` until it recovers.
    #
    # Usage limits (429) and overloads (529) are deliberately NOT triggers: they
    # arrive as HTTP responses, which prove the network works. Failing over on
    # them loaded a qwen tier into the Ollama server the llm-jury council needs
    # and started a fight for GPU memory. See src/proxy/failover.py.
    #
    # Which local profile is picked by the session's estimated input tokens
    # (see FAILOVER_LADDER), measured AFTER bare-mode stripping — the size that
    # decides the tier is the size the local model actually has to prefill.
    failover_to_local: bool = True
    failover_profile: str = "local-failover-qwen27"  # default tier
    # 2, not 3. Measured 2026-08-09 against a dead upstream: Claude Code retries
    # persistently with backoff (9+ attempts over 107s), so reaching a threshold
    # is never the problem — the wait is. Three failures cost ~15-20s before the
    # breaker even asks whether the host is offline, and the model then needs
    # ~10s to load, so the user stares at errors for ~30s.
    #
    # Dropping to 2 is safe because the threshold was never the real guard: the
    # connectivity probe is. A run of failures only opens the breaker if a TCP
    # probe to a public address also fails, so a single transient blip still
    # cannot claim the GPU. The third failure bought latency, not safety.
    failover_threshold: int = 2
    failover_window_seconds: float = 120.0
    failover_probe_seconds: float = 60.0

    # Bare mode: strip the Claude Code harness (system prompt, tool definitions,
    # tool results, images) off a failed-over request, keeping only the tools
    # named in `failover_keep_tools`. See src/proxy/bare.py for why this is the
    # load-bearing change — it is what lets the failover tier be a 14B instead
    # of a 4B without repeating the 2026-07-09 prefill regression.
    # "local" keeps every tool NOT prefixed `mcp__`: Read, Edit, Bash, Glob and
    # Grep all work with no network, so the failover model can keep doing work,
    # while remote MCP integrations (which are dead for as long as the breaker
    # is open, and which are where the ~286K tokens of definitions came from)
    # are dropped. Set to "" for a tier that cannot accept tool definitions at
    # all — deepseek-r1 makes Ollama 400 the request. See src/proxy/bare.py.
    failover_bare: bool = True
    failover_keep_tools: str = "local"
    failover_tool_result_chars: int = 2000

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
    "qwen": "local-failover-qwen27",  # qwen3.5:27b-bare — same as backdoor
    "qwen-fast": "local-fast",    # qwen3.5:4b-64k — lean
    "qwen-9b": "local-qwen-9b",   # qwen3.5:9b-64k — stronger brain for subagents
}


# Cloud→local failover ladder: pick the local profile whose context window fits
# the failed-over session. Each entry is (max_input_tokens_inclusive, profile);
# the first entry whose bound is >= the estimated input token count wins, else
# the last. Bounds sit below each tag's real window to leave room for the reply.
#
# The bounds are measured AFTER bare-mode stripping, which is what makes this
# ladder look so different from the all-4B one it replaces. That ladder existed
# because the harness made every failed-over session enormous: the tiers were
# 64K/128K/256K windows on a 4B, and the model was shrunk 9B → 4B on 2026-07-09
# purely to keep prefill tolerable at that size. Stripping the harness attacks
# the context instead of the model, so the common case is now a small prompt on
# a much stronger model.
#
# The 4B 256K tier is kept as the escape hatch. Bare mode bounds the system
# prompt and tool traffic but NOT the conversation, and a long enough transcript
# still overflows the 27B's 32K window — at which point a weaker model that
# retains the session beats a stronger one that truncates it.
FAILOVER_LADDER: list[tuple[float, str]] = [
    (28_000, "local-failover-qwen27"),      # qwen3.5:27b-bare (32K window, tools)
    (float("inf"), "local-failover-256k"),  # qwen3.5:4b-256k (~262K window)
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
