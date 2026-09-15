"""Model choices must stay on 4B unless the caller selects 27B."""

import os
from pathlib import Path
import subprocess

import pytest

from src.proxy.config import (
    FAILOVER_LADDER, MODEL_ROUTES, Settings, load_profile_settings,
    pick_failover_profile, resolve_model_route,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("typed", ["qwen", "Qwen", "  QWEN  "])
def test_default_alias_loads_a_bare_4b_profile(typed):
    settings = load_profile_settings(resolve_model_route(typed))
    assert settings.provider_model == "qwen3.5:4b-64k"
    assert settings.route_bare
    assert settings.provider_context_tokens == 65536
    assert settings.route_max_input_tokens + settings.provider_max_tokens < 65536
    assert "mcp__hypercrawl__" in settings.failover_keep_tools


def test_all_automatic_tiers_use_4b():
    for _, profile in FAILOVER_LADDER:
        assert ":4b-" in load_profile_settings(profile).provider_model
    assert load_profile_settings(Settings().failover_profile).provider_model == Settings().codex_local_model


@pytest.mark.parametrize("size", [0, 27000, 27001, 54000, 54001, 200000, 10000000])
def test_failover_never_selects_27b(size):
    assert ":4b-" in load_profile_settings(pick_failover_profile(size)).provider_model


def test_only_explicit_27b_aliases_select_the_heavy_model():
    for alias, profile in MODEL_ROUTES.items():
        if ":27b" in load_profile_settings(profile).provider_model:
            assert "27b" in alias
    assert resolve_model_route(" Qwen 27B ") == "local-qwen38-obliterated"


@pytest.mark.parametrize("args,profile,context", [
    ([], "local-qwen4b", "64000"),
    (["lean"], "local-qwen4b", "64000"),
    (["fast"], "local-fast", "64000"),
    (["full"], "local-qwen35", "64000"),
    (["27b"], "local-qwen38-obliterated", "32000"),
    (["27B", "--resume"], "local-qwen38-obliterated", "32000"),
])
def test_launcher_selects_profile_before_any_service_actions(args, profile, context):
    # Execute the real argument parser and budget selection, stopping before
    # Ollama probes, warmup, or bd switch/start. No live router is touched.
    prefix = (ROOT / "qwen").read_text().split("# 1. Make sure", 1)[0]
    script = prefix + '\nprintf "%s:%s" "$PROFILE" "$CLAUDE_CODE_MAX_CONTEXT_TOKENS"\n'
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("QWEN_", "CLAUDE_CODE_MAX_"))}
    result = subprocess.run(
        ["bash", "-c", script, str(ROOT / "qwen"), *args],
        env=env, check=True, capture_output=True, text=True,
    )
    assert result.stdout == f"{profile}:{context}"
