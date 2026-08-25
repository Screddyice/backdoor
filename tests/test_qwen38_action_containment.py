"""The qwen38-action tier is reachable by name and by nothing else.

Qwen3.8-27B Action-Abliterated is a reduced-refusal model: its own card records
92.5% HarmBench direct-request ASR and lists unsupervised tool use as
out-of-scope. It is wired into MODEL_ROUTES so a human can type
`/model qwen38-action`, and deliberately kept out of every path that selects a
model WITHOUT someone typing its name — offline failover and the `model: qwen`
subagents (local-explore, local-worker) that run unattended with Bash and Write.

These tests exist so that containment is a build failure rather than a comment.
If you are here because one of them went red, the question to answer first is
whether an unattended caller can now reach this model, not how to make the
assertion pass.
"""

from pathlib import Path

from src.proxy.config import FAILOVER_LADDER, MODEL_ROUTES, Settings

ACTION_PROFILE = "local-qwen38-action"
PROFILES = Path(__file__).resolve().parents[1] / "profiles"


def test_reachable_by_explicit_name() -> None:
    assert MODEL_ROUTES["qwen38-action"] == ACTION_PROFILE


def test_not_the_default_qwen_route() -> None:
    """`/model qwen` and the `qwen` wrapper must land on the stock heavy tier."""
    assert MODEL_ROUTES["qwen"] != ACTION_PROFILE
    assert MODEL_ROUTES["qwen"] == "local-failover-heavy"


def test_not_the_failover_profile() -> None:
    """An offline host must never silently fail over onto this model."""
    assert Settings().failover_profile != ACTION_PROFILE


def test_not_in_the_failover_ladder() -> None:
    """No session size may escalate or de-escalate into this model."""
    assert ACTION_PROFILE not in {profile for _, profile in FAILOVER_LADDER}


def test_profile_does_not_point_at_ollama() -> None:
    """MLX 4-bit under Ollama loads a 262144 window and eats the host.

    Ollama's MLX engine ignores num_ctx; mlx_vlm.server honors --max-kv-size.
    The panic post-mortem is in modelfiles/bare/qwen3.8-27b-bare.Modelfile.
    """
    env = (PROFILES / f"{ACTION_PROFILE}.env").read_text()
    settings = [
        line for line in env.splitlines() if line and not line.startswith("#")
    ]
    assert "PROVIDER_BASE_URL=http://127.0.0.1:8080/v1" in settings
    assert not [line for line in settings if "11434" in line]


def test_profile_strips_the_harness() -> None:
    """Cold prefill is ~50 tokens/s; an unstripped harness is a ~10min turn."""
    env = (PROFILES / f"{ACTION_PROFILE}.env").read_text()
    assert "ROUTE_BARE=true" in env
    assert "ROUTE_MAX_INPUT_TOKENS=28000" in env
