"""The default Qwen route uses the standalone obliterated GGUF tier.

Claude Code compaction exposed a backend-specific failure in the MLX action
model: its summary request generated only an inline thinking block, which the
proxy stripped to an empty response.  The stock Qwen3.8 GGUF path did not have
that failure.  These tests pin the replacement to Ollama's llama.cpp engine,
keep the action-tuned MLX model reachable by its explicit alias, and retain the
28K escalation guard used by long-running local sessions.
"""

from pathlib import Path

from src.proxy import mlx_admin
from src.proxy.config import FAILOVER_LADDER, MODEL_ROUTES, Settings


PROFILE = "local-qwen38-obliterated"
ACTION_PROFILE = "local-qwen38-action"
MODEL = "qwen3.8:27b-obliterated"
ROOT = Path(__file__).resolve().parents[1]


def _settings_lines(profile: str) -> list[str]:
    env = (ROOT / "profiles" / f"{profile}.env").read_text()
    return [line for line in env.splitlines() if line and not line.startswith("#")]


def test_default_qwen_route_uses_the_obliterated_gguf() -> None:
    assert MODEL_ROUTES["qwen"] == PROFILE
    assert MODEL_ROUTES["qwen38-obliterated"] == PROFILE


def test_action_tuned_mlx_model_remains_an_explicit_rollback() -> None:
    assert MODEL_ROUTES["qwen38-action"] == ACTION_PROFILE


def test_normal_failover_uses_the_obliterated_gguf() -> None:
    assert Settings().failover_profile == PROFILE
    assert FAILOVER_LADDER[0] == (27_000, PROFILE)


def test_profile_uses_local_ollama_without_thinking_stripping() -> None:
    settings = _settings_lines(PROFILE)
    assert "PROVIDER_BASE_URL=http://localhost:11434/v1" in settings
    assert f"PROVIDER_MODEL={MODEL}" in settings
    assert f"RUNTIME_PROFILE={PROFILE}" in settings
    assert "ROUTE_BARE=true" in settings
    assert "ROUTE_MAX_INPUT_TOKENS=27000" in settings
    assert "PROVIDER_MAX_TOKENS=4096" in settings
    assert not [line for line in settings if line.startswith("PROVIDER_STRIP_INLINE_THINKING=")]


def test_modelfile_clamps_context_and_installs_the_tool_template() -> None:
    text = (ROOT / "modelfiles" / "bare" / "qwen3.8-27b-obliterated.Modelfile").read_text()
    assert "FROM hf.co/OBLITERATUS/Qwen3.8-27B-OBLITERATED:Q4_K_M" in text
    assert "PARAMETER num_ctx 32768" in text
    assert "PARAMETER repeat_penalty 1.15" in text
    assert "TEMPLATE " in text
    assert ".Tools" in text
    assert "<tool_call>" in text
    assert ".ToolCalls" in text


def test_ollama_27b_profile_is_memory_exclusive_with_mlx() -> None:
    exclusive = getattr(mlx_admin, "OLLAMA_EXCLUSIVE_PROFILES", set())
    assert PROFILE in exclusive


def test_qwen_wrapper_defaults_to_the_obliterated_profile_at_32k() -> None:
    wrapper = (ROOT / "qwen").read_text()
    assert 'PROFILE="local-qwen38-obliterated"' in wrapper
    assert "local-qwen38-obliterated) QWEN_CTX=32000" in wrapper
    assert '[ "$PROFILE" = "local-qwen38-obliterated" ] && KEEP_ALIVE="10m"' in wrapper


def test_qwen_wrapper_stops_mlx_before_warming_the_ollama_27b() -> None:
    wrapper = (ROOT / "qwen").read_text()
    stop = wrapper.index("qwen38 stop")
    warm = wrapper.index('http://127.0.0.1:11434/api/chat')
    assert stop < warm
    assert 'WARM_OLLAMA=""' in wrapper
    assert '[ -n "$WARM_OLLAMA" ] && curl' in wrapper
