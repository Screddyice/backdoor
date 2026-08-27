"""`/model Qwen` must resolve, and the MLX tier must not co-reside with Ollama.

Two independent failures, both silent:

  * MODEL_ROUTES.get(model) is a plain dict lookup, so `/model Qwen` missed and
    the session stayed on the cloud model. Nothing reported it, because a
    passthrough is a legitimate outcome -- the user just kept paying cloud
    tokens believing they were local.

  * The MLX Qwen3.8-27B tier holds ~17 GB and an llm-jury council holds ~21 GB.
    This host has 36 GB with a wired ceiling near 27, so they cannot co-reside;
    it has kernel-panicked the machine twice. The only guard was a comment.
"""

from __future__ import annotations

import pytest

from src.proxy.config import MODEL_ROUTES, resolve_model_route
from src.proxy import mlx_admin, ollama_admin

# conftest.py installs an AUTOUSE fixture that replaces mlx_admin.resolve_profile
# with an identity function, so tests do not probe 127.0.0.1:8080. That is right
# for every other test and fatal for these: calling the patched name would
# exercise nothing and assert nothing while still passing. Capture the real
# function at import time -- module import happens at collection, before any
# fixture runs -- and call it directly below.
_REAL_RESOLVE_PROFILE = mlx_admin.resolve_profile


@pytest.mark.parametrize("typed", ["qwen", "Qwen", "QWEN", "  Qwen  ", "QwEn"])
def test_model_qwen_resolves_however_it_is_capitalised(typed):
    assert resolve_model_route(typed) == MODEL_ROUTES["qwen"]


@pytest.mark.parametrize("typed", ["Qwen-Fast", "QWEN-9B", "Qwen38-Action"])
def test_the_other_local_tiers_are_case_insensitive_too(typed):
    assert resolve_model_route(typed) == MODEL_ROUTES[typed.strip().lower()]


def test_an_unknown_model_still_passes_through():
    # A cloud model name must NOT be captured by the local router.
    assert resolve_model_route("claude-opus-5") is None
    assert resolve_model_route("") is None
    assert resolve_model_route(None) is None


@pytest.mark.anyio
async def test_engaging_the_mlx_tier_evicts_resident_ollama_models(monkeypatch):
    evicted: list[str] = []

    async def fake_resident(base_url=ollama_admin.DEFAULT_OLLAMA_BASE):
        return ["gemma3:12b", "llama3.1:8b"]

    async def fake_unload(base_url, model):
        evicted.append(model)
        return True

    async def running():
        return True

    monkeypatch.setattr(ollama_admin, "resident_models", fake_resident)
    monkeypatch.setattr(ollama_admin, "unload", fake_unload)
    monkeypatch.setattr(mlx_admin, "ensure_running", running)

    assert await _REAL_RESOLVE_PROFILE(mlx_admin.MLX_PROFILE) == mlx_admin.MLX_PROFILE
    # The council is gone before the 17 GB tier serves.
    assert evicted == ["gemma3:12b", "llama3.1:8b"]


@pytest.mark.anyio
async def test_a_non_mlx_profile_never_touches_ollama_residency(monkeypatch):
    called = False

    async def fake_resident(base_url=ollama_admin.DEFAULT_OLLAMA_BASE):
        nonlocal called
        called = True
        return ["gemma3:12b"]

    monkeypatch.setattr(ollama_admin, "resident_models", fake_resident)
    # A 4B tier co-resides with a council perfectly well; evicting would be
    # gratuitous churn.
    assert await _REAL_RESOLVE_PROFILE("local-fast") == "local-fast"
    assert called is False


@pytest.mark.anyio
async def test_an_eviction_failure_does_not_fail_the_request(monkeypatch):
    async def boom(base_url=ollama_admin.DEFAULT_OLLAMA_BASE):
        raise RuntimeError("ollama unreachable")

    async def running():
        return True

    monkeypatch.setattr(ollama_admin, "resident_models", boom)
    monkeypatch.setattr(mlx_admin, "ensure_running", running)

    # Refusing to answer would trade a memory risk for a certain outage.
    assert await _REAL_RESOLVE_PROFILE(mlx_admin.MLX_PROFILE) == mlx_admin.MLX_PROFILE


@pytest.mark.anyio
async def test_nothing_resident_means_nothing_evicted(monkeypatch):
    unloads = 0

    async def none_resident(base_url=ollama_admin.DEFAULT_OLLAMA_BASE):
        return []

    async def fake_unload(base_url, model):
        nonlocal unloads
        unloads += 1
        return True

    monkeypatch.setattr(ollama_admin, "resident_models", none_resident)
    monkeypatch.setattr(ollama_admin, "unload", fake_unload)
    assert await ollama_admin.evict_all() == []
    assert unloads == 0
