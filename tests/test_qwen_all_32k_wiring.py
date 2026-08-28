"""Pin every qwen wrapper mode to a usable 32K context."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _profile(name: str) -> str:
    return (ROOT / "profiles" / f"{name}.env").read_text()


def test_wrapper_caps_the_default_and_full_profiles_at_32k() -> None:
    wrapper = (ROOT / "qwen").read_text()
    assert "local-qwen35|local-fast) QWEN_CTX=32000" in wrapper
    assert "local-qwen35|local-fast) QWEN_CTX=64000" not in wrapper
    assert 'export CLAUDE_CODE_MAX_CONTEXT_TOKENS="$QWEN_CTX"' in wrapper


def test_full_mode_uses_the_lean_launcher() -> None:
    wrapper = (ROOT / "qwen").read_text()
    assert 'MODE_NOTE="full-lean(4b)"' in wrapper
    assert 'MODE_NOTE="full-hooks(harness)"' not in wrapper
    assert 'EXTRA_ARGS=(--settings "$HOME/backdoor/hook-mode.settings.json")' not in wrapper


def test_4b_profiles_use_a_real_32k_ollama_tag() -> None:
    for name in ("local-qwen35", "local-fast"):
        profile = _profile(name)
        assert "PROVIDER_MODEL=qwen3.5:4b-32k" in profile
        assert "PROVIDER_MAX_TOKENS=4096" in profile
        assert "ROUTE_BARE=true" in profile
        assert "ROUTE_MAX_INPUT_TOKENS=27000" in profile

    modelfile = (ROOT / "modelfiles" / "bare" / "qwen3.5-4b-32k.Modelfile").read_text()
    assert "FROM qwen3.5:4b" in modelfile
    assert "PARAMETER num_ctx 32768" in modelfile


def test_cognee_stays_enabled_for_lean_modes() -> None:
    wrapper = (ROOT / "qwen").read_text()
    assert "COGNEE_DEFAULT=1" in wrapper
    assert "cognee-mcp-shim.py" in wrapper
    assert "local memory, local inference" in wrapper
