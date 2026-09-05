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


@pytest.mark.asyncio
async def test_stream_retry_replays_existing_flight_after_a_stale_cache_miss(runtime, monkeypatch):
    factory_started = asyncio.Event()
    release_factory = asyncio.Event()
    cache_reads = 0
    calls = 0

    async def stale_cache_read(_key):
        nonlocal cache_reads
        cache_reads += 1
        if cache_reads == 2:
            await asyncio.Event().wait()
        return None

    async def factory():
        nonlocal calls
        calls += 1
        yield "one"
        factory_started.set()
        await release_factory.wait()
        yield "two"

    monkeypatch.setattr(runtime, "_cached_stream", stale_cache_read)
    first = asyncio.create_task(collect(runtime.stream_once("same", factory)))
    await factory_started.wait()
    second = asyncio.create_task(collect(runtime.stream_once("same", factory)))
    release_factory.set()

    assert await asyncio.wait_for(first, timeout=1) == ["one", "two"]
    assert await asyncio.wait_for(second, timeout=1) == ["one", "two"]
    assert calls == 1
    assert cache_reads == 1


@pytest.mark.asyncio
async def test_stream_cancellation_during_cache_persistence_notifies_subscribers(runtime, monkeypatch):
    persistence_started = asyncio.Event()

    async def wait_to_persist(_key, _events):
        persistence_started.set()
        await asyncio.Event().wait()

    async def factory():
        yield "one"

    monkeypatch.setattr(runtime, "_store_stream", wait_to_persist)
    consumer = asyncio.create_task(collect(runtime.stream_once("same", factory)))
    await persistence_started.wait()

    runtime.close()

    with pytest.raises(asyncio.CancelledError):
        await consumer


@pytest.mark.asyncio
async def test_prepare_local_fallback_keeps_optional_tail_below_target(runtime, adapter):
    fallback_context = adapter.normalize(
        MessagesRequest.model_validate(
            {
                "model": "qwen",
                "messages": [
                    {"role": "user", "content": "old optional history " * 150},
                    {"role": "user", "content": "current-marker " + "x" * 16_000},
                ],
            }
        )
    )
    runtime.store.archive = Mock(side_effect=ContextStoreUnavailable())

    prepared = await runtime.prepare_local(
        fallback_context,
        adapter,
        Settings(
            _env_file=None,
            context_virtualization=True,
            context_target_input_tokens=18_000,
            context_hard_input_tokens=22_000,
        ),
    )

    assert prepared.token_count <= 18_000
    assert "current-marker" in prepared.payload.model_dump_json()
    assert "old optional history" not in prepared.payload.model_dump_json()


@pytest.mark.asyncio
async def test_prepare_local_prunes_after_successful_archive(runtime, adapter, context):
    runtime.store.prune_if_needed = Mock(return_value=0)

    await runtime.prepare_local(context, adapter, Settings(_env_file=None, context_virtualization=True))

    runtime.store.prune_if_needed.assert_called_once()


@pytest.mark.asyncio
async def test_slow_stream_subscriber_is_terminated_without_blocking_active_subscribers(
    tmp_path,
):
    settings = Settings(_env_file=None, context_response_cache_seconds=0)
    runtime = ContextRuntime(
        ContextStore(tmp_path / "private" / "transcripts.sqlite3"),
        QwenTokenGate(executable="/missing/llama-tokenize", model_path="/missing/model.gguf"),
        settings,
        stream_subscriber_queue_size=1,
    )
    release_factory = asyncio.Event()

    async def factory():
        yield "one"
        await release_factory.wait()
        for event in ("two", "three", "four"):
            yield event
            await asyncio.sleep(0)

    stalled = runtime.stream_once("same", factory)
    assert await anext(stalled) == "one"
    active = asyncio.create_task(collect(runtime.stream_once("same", factory)))
    await asyncio.sleep(0)
    release_factory.set()

    assert await active == ["one", "two", "three", "four"]
    with pytest.raises(RuntimeError, match="fell behind"):
        await anext(stalled)


@pytest.mark.asyncio
async def test_completed_streams_release_key_locks_without_breaking_same_key_replay(runtime):
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        yield "event"

    results = await asyncio.gather(
        *(collect(runtime.stream_once(f"key-{index}", factory)) for index in range(20))
    )
    first = await collect(runtime.stream_once("same", factory))
    retry = await collect(runtime.stream_once("same", factory))

    assert results == [["event"]] * 20
    assert first == retry == ["event"]
    assert calls == 21
    assert runtime._stream_locks == {}
