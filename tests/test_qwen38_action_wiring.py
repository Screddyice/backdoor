"""The action-tuned MLX tier remains reachable as an explicit rollback.

Qwen3.8-27B Action-Abliterated is a bounded refusal-direction ablation (92.5%
HarmBench direct-request ASR per its own card).  It moved off the unattended
paths after its MLX backend returned an empty compaction summary, but the
measured action checkpoint stays available by name.

The load-bearing test in this file is test_fallback_profile_is_a_lazy_ollama_tier.
Unlike every other local tier, this one does not load on demand: it is a launchd
job that is either up or absent. Failover fires when the host is offline and
nobody is watching, so a tier that cannot self-start would turn the fallback into
a dead session. mlx_admin starts it and drops to the 9B when it cannot.
"""

from pathlib import Path

from src.proxy import mlx_admin
from src.proxy.config import MODEL_ROUTES

ACTION_PROFILE = "local-qwen38-action"
FALLBACK_PROFILE = "local-failover-heavy"
PROFILES = Path(__file__).resolve().parents[1] / "profiles"


def test_reachable_by_its_own_name_too() -> None:
    assert MODEL_ROUTES["qwen38-action"] == ACTION_PROFILE


def test_stock_refusal_tier_is_still_reachable() -> None:
    """`/model qwen-stock` is the documented way back to an unablated model."""
    assert MODEL_ROUTES["qwen-stock"] == FALLBACK_PROFILE


def test_fallback_profile_is_a_lazy_ollama_tier() -> None:
    """The fallback must load on demand, or it is not a fallback.

    If someone points MLX_FALLBACK_PROFILE at another supervised server, an
    offline host with both servers stopped gets nothing at all.
    """
    assert mlx_admin.MLX_FALLBACK_PROFILE == FALLBACK_PROFILE
    env = (PROFILES / f"{FALLBACK_PROFILE}.env").read_text()
    assert "PROVIDER_BASE_URL=http://localhost:11434/v1" in env.splitlines()


def test_profile_does_not_point_at_ollama() -> None:
    """MLX 4-bit under Ollama loads a 262144 window and eats the host.

    Ollama's MLX engine ignores num_ctx; mlx_vlm.server honors --max-kv-size.
    The panic post-mortem is in modelfiles/bare/qwen3.8-27b-bare.Modelfile.
    """
    settings = _settings_lines(ACTION_PROFILE)
    assert "PROVIDER_BASE_URL=http://127.0.0.1:8080/v1" in settings
    assert not [line for line in settings if "11434" in line]


def test_profile_strips_the_harness() -> None:
    """Cold prefill is ~50 tokens/s; an unstripped harness is a ~10min turn."""
    settings = _settings_lines(ACTION_PROFILE)
    assert "ROUTE_BARE=true" in settings
    assert "ROUTE_MAX_INPUT_TOKENS=28000" in settings


def _settings_lines(profile: str) -> list[str]:
    env = (PROFILES / f"{profile}.env").read_text()
    return [line for line in env.splitlines() if line and not line.startswith("#")]
