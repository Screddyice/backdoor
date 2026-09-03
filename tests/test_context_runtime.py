from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from src.proxy.claude_context_adapter import ClaudeContextAdapter
from src.proxy.config import Settings
from src.proxy.context_runtime import ContextRuntime
from src.proxy.context_store import ContextStore, ContextStoreUnavailable
from src.proxy.context_tokenizer import QwenTokenGate
from src.proxy.models import MessagesRequest


async def collect(stream):
    return [event async for event in stream]


@pytest.fixture
def adapter() -> ClaudeContextAdapter:
    return ClaudeContextAdapter()


@pytest.fixture
def context(adapter):
    return adapter.normalize(
        MessagesRequest.model_validate(
            {
                "model": "qwen",
                "messages": [
                    {"role": "user", "content": "old context " * 3_000},
                    {"role": "assistant", "content": "old answer " * 3_000},
                    {"role": "user", "content": "current instruction"},
                ],
            }
        )
    )


@pytest.fixture
def runtime(tmp_path) -> ContextRuntime:
    settings = Settings(
        _env_file=None,
        context_virtualization=True,
        context_target_input_tokens=18_000,
        context_hard_input_tokens=22_000,
    )
    store = ContextStore(tmp_path / "private" / "transcripts.sqlite3")
    gate = QwenTokenGate(executable="/missing/llama-tokenize", model_path="/missing/model.gguf")
    return ContextRuntime(store, gate, settings, archive_queue_size=1)


@pytest.mark.asyncio
async def test_cloud_archive_never_blocks_when_queue_is_full(runtime, context):
    runtime._archive_queue.put_nowait(context)

    runtime.archive_cloud(context)

    assert runtime.archive_dropped == 1


@pytest.mark.asyncio
async def test_prepare_local_uses_recent_tail_when_store_fails(runtime, adapter, context):
    runtime.store.archive = Mock(side_effect=ContextStoreUnavailable())

    prepared = await runtime.prepare_local(
        context, adapter, Settings(_env_file=None, context_virtualization=True)
    )

    assert prepared.token_count <= 22_000
    assert prepared.used_store is False
    assert "current instruction" in prepared.payload.model_dump_json()


@pytest.mark.asyncio
async def test_identical_complete_retries_share_one_factory(runtime):
    factory = AsyncMock(return_value={"id": "msg_local"})

    results = await asyncio.gather(
        runtime.run_complete_once("same", factory),
        runtime.run_complete_once("same", factory),
    )

    assert results == [{"id": "msg_local"}, {"id": "msg_local"}]
    factory.assert_awaited_once()


@pytest.mark.asyncio
async def test_identical_stream_retries_share_one_generation(runtime):
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        for event in ("one", "two"):
            yield event

    left, right = await asyncio.gather(
        collect(runtime.stream_once("same", factory)),
        collect(runtime.stream_once("same", factory)),
    )

    assert left == right == ["one", "two"]
    assert calls == 1
