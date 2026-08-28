import os
from functools import lru_cache
from pydantic import Field, model_validator
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

    # Strip inline <think>...</think> out of streamed content for backends that
    # leave the tags in `content` instead of filling a `reasoning` field.
    # Ollama fills `reasoning` and honors reasoning_effort, so it needs nothing.
    # mlx_vlm.server does neither, and Qwen's chat template PRE-FILLS the opening
    # <think> into the assistant turn, so the stream begins inside the block and
    # the first thing a user sees is the model's reasoning followed by a bare
    # </think>.
    #
    # Opt-in per profile rather than always-on. Knowing the stream starts inside
    # a thinking block is what makes this free: we can route deltas to a thinking
    # block immediately. Detecting it generically would mean buffering every
    # stream until a closer showed up, which would delay first paint for every
    # tier to fix one.
    provider_strip_inline_thinking: bool = False

    # Idle residency to clamp this tier to after serving a FAILOVER request, as
    # an Ollama duration ("45s"). Empty = leave it on Ollama's global
    # OLLAMA_KEEP_ALIVE (5m here). Local Ollama profiles only; see
    # src/proxy/ollama_admin.py for why it needs a separate native-API call.
    #
    # Set it on tiers that exist ONLY to catch an outage. Do NOT set it on a
    # tier reachable through MODEL_ROUTES: a deliberate `/model qwen` session
    # that thinks for longer than the clamp would evict its own 17 GB model and
    # reload it on the next turn, which is slower and *more* memory churn than
    # leaving it resident.
    provider_keep_alive: str = ""

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
    # 2026-08-25: the abliterated 27B on MLX, by explicit decision. It is not
    # lazily loaded like the Ollama tiers, so routes.py starts it on demand and
    # drops to local-failover-heavy (9B) when it will not come up. See
    # src/proxy/mlx_admin.py.
    failover_profile: str = "local-qwen38-obliterated"  # default tier
    # Probe on the first transport failure. The connectivity probe remains the
    # safety gate: local failover opens only when the host is offline. Waiting
    # for a second Claude retry exposed one avoidable API error before asking
    # the question that decides whether local failover is allowed.
    #
    # Dropping to 1 is safe because the threshold was never the real guard: the
    # connectivity probe is. A run of failures only opens the breaker if a TCP
    # probe to a public address also fails, so a single transient blip still
    # cannot claim the GPU. The third failure bought latency, not safety.
    failover_threshold: int = 1
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

    # Durable-memory recall injected into LOCAL model prompts, read from the
    # offline mirror at ~/.mem0-local/cache.db. See src/proxy/memory.py.
    #
    # On by default because the tier that needs it most is the one that had
    # nothing: the `qwen` wrapper's lean/fast modes pass `--bare`, which disables
    # every hook, including the `UserPromptSubmit` recall that gives every other
    # session its memory. Cloud requests are excluded at the call site, so this
    # cannot double-inject for a session whose hook already ran.
    #
    # The budget is deliberately small. Bare mode exists to keep the prompt near
    # 945 tokens against a 32K window, and unbounded recall would rebuild the
    # problem it was created to solve.
    memory_inject: bool = True
    memory_top_k: int = 6
    memory_char_budget: int = 1200

    # Authoritative local Cognee recall for fresh Codex failover turns. The API
    # key is optional on a single-user server and must arrive through the process
    # environment, never a committed profile.
    cognee_base_url: str = "http://127.0.0.1:8001"
    cognee_api_key: str = ""
    codex_cognee_timeout_seconds: float = Field(default=2.0, gt=0)
    codex_cognee_top_k: int = Field(default=8, ge=1)
    codex_cognee_char_budget: int = Field(default=8_000, ge=1)

    # Codex Responses failover keeps a hard 32K window on both the cloud guard
    # and the fresh local request. Component budgets include the reply reserve,
    # so an invalid allocation is rejected at startup rather than at inference.
    codex_context_window: int = Field(default=32_000, ge=1)
    codex_reply_reserve_tokens: int = Field(default=4_000, ge=1)
    codex_system_budget_tokens: int = Field(default=1_000, ge=0)
    codex_memory_budget_tokens: int = Field(default=2_000, ge=0)
    codex_tools_budget_tokens: int = Field(default=4_000, ge=0)
    codex_active_turn_budget_tokens: int = Field(default=21_000, ge=1)
    codex_local_model: str = "qwen3.8:27b-obliterated"
    codex_local_responses_url: str = "http://127.0.0.1:11434/v1/responses"
    codex_local_tools: str = "local"
    codex_failover_to_local: bool = True
    codex_chatgpt_upstream: str = "https://chatgpt.com/backend-api/codex"
    codex_failover_threshold: int = Field(default=3, ge=1)
    codex_failover_window_seconds: float = Field(default=120.0, gt=0)
    codex_failover_probe_seconds: float = Field(default=60.0, gt=0)
    codex_failover_statuses: str = "429,500,502,503,504,529"
    codex_failover_require_offline: bool = False

    @model_validator(mode="after")
    def validate_codex_context_allocation(self):
        allocated = (
            self.codex_reply_reserve_tokens
            + self.codex_system_budget_tokens
            + self.codex_memory_budget_tokens
            + self.codex_tools_budget_tokens
            + self.codex_active_turn_budget_tokens
        )
        if allocated > self.codex_context_window:
            raise ValueError("Codex context component budgets exceed the context window")
        return self

    # Bare mode for an EXPLICIT `/model <name>` route, not just failover.
    # MODEL_ROUTES hits skip the failover branch entirely, so before this flag a
    # deliberate `/model qwen` handed the full unstripped harness to whatever
    # tier the route named. That is fine for the 64K tiers and wrong for a tier
    # whose window assumes bare mode: the default tier is bounded at 27K, and a full
    # harness session does not fit — the same over-window regression the
    # profile header warns about.
    # Set ROUTE_BARE=true on those profiles only. Deliberately reuses the
    # failover_* keep-list and truncation knobs so both paths strip identically;
    # a route that strips differently from failover would be a second behaviour
    # to keep in sync for no benefit.
    route_bare: bool = False

    # Largest post-strip session this tier will accept on an explicit
    # `/model <name>` route. 0 disables the check.
    #
    # ROUTE_BARE bounds the system prompt and tool traffic; it does NOT bound the
    # transcript, and the transcript is the half that grows. A long-lived `qwen`
    # session therefore walks past its own window with nothing to stop it, and
    # because MODEL_ROUTES is a static dict it never consults FAILOVER_LADDER —
    # the one place that would have handed it to a wider tier. Measured
    # 2026-08-12: a `qwen` session sent 143,490 tokens at the 27B's 32K window 87
    # times over ~17 hours, failing and retrying every 5-10 minutes, loading 23GB
    # of a 36GB host on each attempt. The window was configured correctly; there
    # was simply no route from "too big for this tier" to "use the wider one".
    #
    # Set this to the same bound the tier carries in FAILOVER_LADDER so a
    # deliberate route and a failover size the tier identically. Over it, the
    # request escalates through that ladder instead of failing.
    route_max_input_tokens: int = 0

    # Optional runtime identity for profiles whose server lifecycle must be
    # supervised before a request can load weights. Unlike the hybrid router's
    # local `profile` variable, this survives `bd switch` into profile mode.
    runtime_profile: str = ""

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

    # CONNECT forward proxy — lets Remote Control and the router coexist.
    #
    # Claude Code refuses Remote Control unless ANTHROPIC_BASE_URL is unset or
    # points at api.anthropic.com (exact host allowlist; the
    # _CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL escape hatch is explicitly
    # excluded from RC). Naming this router as the base URL therefore bought
    # `/model qwen` and failover at the cost of Remote Control.
    #
    # It only ever needed to be IN the path, not NAMED as the endpoint. Claude
    # Code honours HTTPS_PROXY, so pointing that here and leaving the base URL
    # unset satisfies both: the RC gate sees a clean environment, and every
    # request still crosses this process. See src/proxy/forward.py.
    #
    # Off by default: it mints a local CA on first run, which should be an
    # opt-in for anyone running backdoor for its original purpose.
    forward_proxy: bool = False
    forward_host: str = "127.0.0.1"
    forward_port: int = 8084

    # Hosts to TLS-terminate and hand to the router. Everything absent from
    # this list is tunnelled opaquely — including the Remote Control bridge on
    # claude.ai, which must not be intercepted.
    forward_mitm_hosts: str = "api.anthropic.com"

    # Where the intercepted plaintext is delivered. Deliberately NOT `port`
    # above: the hybrid LaunchAgent sets both PORT and FORWARD_ROUTER_PORT to
    # 8083. They remain separate settings because other deployment modes can
    # forward to a router in another process.
    forward_router_host: str = "127.0.0.1"
    forward_router_port: int = 8083

    # Close a tunnel only after both directions have been byte-idle longer than
    # the router's 600-second Anthropic read timeout.
    forward_idle_timeout: float = Field(default=660.0, gt=0, allow_inf_nan=False)
    forward_max_connections: int = Field(default=512, ge=1)

    # Shares ~/.backdoor with the failover breaker's published state.
    forward_ca_dir: str = "~/.backdoor/ca"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache():
    get_settings.cache_clear()


# Model names a Claude Code session can request (via `/model <name>` or
# `--model <name>`) that route to a LOCAL profile in hybrid mode.
def resolve_model_route(model: str | None) -> str | None:
    """MODEL_ROUTES lookup that is not defeated by capitalisation.

    `MODEL_ROUTES.get(model)` is a plain dict lookup, so `/model Qwen` missed
    and the session silently stayed on the cloud model -- the one failure mode
    where nothing is reported, because a passthrough is a legitimate outcome.
    Model names are identifiers a person types, not data, so case is not
    meaningful here. Surrounding whitespace is stripped for the same reason.
    """

    if not model:
        return None
    return MODEL_ROUTES.get(model.strip().lower())


MODEL_ROUTES: dict[str, str] = {
    "qwen": "local-qwen38-obliterated",  # Qwen3.8-27B OBLITERATED GGUF on Ollama
    "qwen-fast": "local-fast",    # qwen3.5:4b-64k — lean
    "qwen-9b": "local-qwen-9b",   # qwen3.5:9b-64k — stronger brain for subagents
    "qwen38-obliterated": "local-qwen38-obliterated",
    # Keep the action-tuned MLX checkpoint reachable as an explicit rollback.
    "qwen38-action": "local-qwen38-action",
    # The stock 9B, still reachable by name. This is what to type when you want
    # a model with its refusal behaviour intact.
    "qwen-stock": "local-failover-heavy",
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
    (27_000, "local-qwen38-obliterated"),   # 27K in + 4K out + template reserve
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
