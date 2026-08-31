"""Retry coalescing, completed cache, and fail-open cloud archiving."""

import asyncio

from src.proxy.context_runtime import ContextRuntime, normalized_request_hash
from src.proxy.context_store import ContextStore
from src.proxy.models import Message, MessagesRequest, Tool


def request(text: str = "same", *, stream: bool = True, metadata=None) -> MessagesRequest:
    return MessagesRequest(
        model="claude-opus-5",
        system="system",
        messages=[Message(role="user", content=text)],
        tools=[Tool(name="Read", input_schema={"type": "object"})],
        stream=stream,
        metadata=metadata,
    )


def test_request_hash_ignores_stream_and_metadata():
    left = request(stream=True, metadata={"trace": "a"})
    right = request(stream=False, metadata={"trace": "b"})

    assert normalized_request_hash(left) == normalized_request_hash(right)
    assert normalized_request_hash(left) != normalized_request_hash(request("changed"))


async def test_ten_identical_stream_retries_share_one_generation(tmp_path):
    runtime = ContextRuntime(ContextStore(tmp_path / "transcripts.sqlite3"))
    calls = 0

    async def generate():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        yield "event: content_block_delta\ndata: stable\n\n"
        yield "event: message_stop\ndata: done\n\n"

    async def collect():
        return [event async for event in runtime.stream_once("hash", generate)]

    results = await asyncio.gather(*(collect() for _ in range(10)))
    await runtime.close()

    assert calls == 1
    assert results == [[
        "event: content_block_delta\ndata: stable\n\n",
        "event: message_stop\ndata: done\n\n",
    ]] * 10


async def test_completed_stream_cache_survives_runtime_restart(tmp_path):
    store = ContextStore(tmp_path / "transcripts.sqlite3")
    first_runtime = ContextRuntime(store, cache_seconds=600, now_fn=lambda: 10.0)
    calls = 0

    async def generate():
        nonlocal calls
        calls += 1
        yield "event: message_stop\ndata: cached\n\n"

    first = [event async for event in first_runtime.stream_once("hash", generate)]
    await first_runtime.close()
    second_runtime = ContextRuntime(store, cache_seconds=600, now_fn=lambda: 20.0)
    second = [event async for event in second_runtime.stream_once("hash", generate)]
    await second_runtime.close()

    assert first == second
    assert calls == 1


async def test_completed_in_memory_stream_closes_cache_read_race(tmp_path, monkeypatch):
    runtime = ContextRuntime(ContextStore(tmp_path / "transcripts.sqlite3"))
    calls = 0

    async def generate():
        nonlocal calls
        calls += 1
        yield "event: message_stop\ndata: cached\n\n"

    first = [event async for event in runtime.stream_once("hash", generate)]

    async def stale_cache_read(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_read_cache", stale_cache_read)
    second = [event async for event in runtime.stream_once("hash", generate)]
    await runtime.close()

    assert second == first
    assert calls == 1


async def test_incomplete_stream_is_not_cached(tmp_path):
    runtime = ContextRuntime(ContextStore(tmp_path / "transcripts.sqlite3"))
    calls = 0

    async def incomplete():
        nonlocal calls
        calls += 1
        yield "event: content_block_delta\ndata: partial\n\n"

    first = [event async for event in runtime.stream_once("hash", incomplete)]
    second = [event async for event in runtime.stream_once("hash", incomplete)]
    await runtime.close()

    assert first == second == ["event: content_block_delta\ndata: partial\n\n"]
    assert calls == 2


async def test_stream_cache_write_failure_does_not_break_completed_response(tmp_path, monkeypatch):
    runtime = ContextRuntime(ContextStore(tmp_path / "transcripts.sqlite3"))

    async def fail_cache(*_args, **_kwargs):
        raise OSError("disk full")

    async def generate():
        yield "event: message_stop\ndata: complete\n\n"

    monkeypatch.setattr(runtime, "_write_cache", fail_cache)
    events = [event async for event in runtime.stream_once("hash", generate)]
    await runtime.close()

    assert events == ["event: message_stop\ndata: complete\n\n"]


async def test_nonstream_retries_share_one_generation(tmp_path):
    runtime = ContextRuntime(ContextStore(tmp_path / "transcripts.sqlite3"))
    calls = 0

    async def generate():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return {"id": "stable"}

    results = await asyncio.gather(
        *(runtime.run_once("hash", generate) for _ in range(10))
    )
    await runtime.close()

    assert calls == 1
    assert results == [{"id": "stable"}] * 10


async def test_nonstream_cache_write_failure_does_not_break_response(tmp_path, monkeypatch):
    runtime = ContextRuntime(ContextStore(tmp_path / "transcripts.sqlite3"))

    async def fail_cache(*_args, **_kwargs):
        raise OSError("disk full")

    async def generate():
        return {"id": "complete"}

    monkeypatch.setattr(runtime, "_write_cache", fail_cache)
    response = await runtime.run_once("hash", generate)
    await runtime.close()

    assert response == {"id": "complete"}


async def test_full_archive_queue_never_blocks_caller(tmp_path, monkeypatch):
    runtime = ContextRuntime(
        ContextStore(tmp_path / "transcripts.sqlite3"),
        archive_queue_size=2,
    )
    archived = []
    monkeypatch.setattr(runtime, "_archive_one", archived.append)

    runtime.archive_cloud(request("one"))
    runtime.archive_cloud(request("two"))
    runtime.archive_cloud(request("three"))
    runtime.archive_cloud(request("four"))
    assert runtime.archive_dropped == 2

    await runtime.close()

    assert [item.messages[0].content for item in archived] == ["one", "two"]


async def test_archive_failure_does_not_escape_worker(tmp_path, monkeypatch):
    runtime = ContextRuntime(ContextStore(tmp_path / "transcripts.sqlite3"))

    def fail(_request):
        raise OSError("disk full")

    monkeypatch.setattr(runtime, "_archive_one", fail)
    runtime.archive_cloud(request())
    await runtime.close()

    assert runtime.archive_failed == 1
