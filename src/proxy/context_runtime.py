"""Fail-open archive work and shared local generations."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
import asyncio
import hashlib
import json
import time
from typing import Any

from .context_store import ContextStore
from .models import MessagesRequest


_STOP = object()
_STREAM_DONE = object()


@dataclass(frozen=True)
class _StreamError:
    error: BaseException


@dataclass
class _SharedStream:
    events: list[str] = field(default_factory=list)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    task: asyncio.Task | None = None


def normalized_request_hash(req: MessagesRequest) -> str:
    """Hash content that defines one model turn, excluding transport fields."""
    payload = {
        "model": req.model,
        "system": req.system,
        "messages": [message.model_dump(mode="json") for message in req.messages],
        "tools": [tool.model_dump(mode="json") for tool in (req.tools or [])],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ContextRuntime:
    def __init__(
        self,
        store: ContextStore,
        *,
        archive_queue_size: int = 32,
        cache_seconds: int = 600,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.archive_queue_size = archive_queue_size
        self.cache_seconds = cache_seconds
        self._now = now_fn
        self._archive_queue: asyncio.Queue = asyncio.Queue(
            maxsize=archive_queue_size
        )
        self._archive_task: asyncio.Task | None = None
        self.archive_dropped = 0
        self.archive_failed = 0
        self.cache_failed = 0
        self._complete_lock = asyncio.Lock()
        self._complete_inflight: dict[str, asyncio.Task] = {}
        self._stream_lock = asyncio.Lock()
        self._streams: dict[str, _SharedStream] = {}
        self._completed_streams: dict[str, tuple[float, tuple[str, ...]]] = {}
        self._closed = False

    @staticmethod
    def _cache_key(request_hash: str, kind: str) -> str:
        return f"{request_hash}:{kind}"

    async def _read_cache(self, request_hash: str, kind: str) -> dict[str, Any] | None:
        value = await asyncio.to_thread(
            self.store.get_cached_response,
            self._cache_key(request_hash, kind),
            self._now(),
        )
        if not isinstance(value, dict) or value.get("kind") != kind:
            return None
        return value

    async def _write_cache(
        self,
        request_hash: str,
        kind: str,
        value: dict[str, Any],
    ) -> None:
        payload = {"kind": kind, **value}
        await asyncio.to_thread(
            self.store.put_cached_response,
            self._cache_key(request_hash, kind),
            payload,
            self._now() + self.cache_seconds,
        )

    def _ensure_archive_worker(self) -> None:
        if self._archive_task is None or self._archive_task.done():
            self._archive_task = asyncio.create_task(self._archive_worker())

    def archive_cloud(self, req: MessagesRequest) -> None:
        """Queue an exact cloud request without waiting for disk."""
        if self._closed:
            return
        self._ensure_archive_worker()
        try:
            self._archive_queue.put_nowait(req.model_copy(deep=True))
        except asyncio.QueueFull:
            self.archive_dropped += 1

    def _archive_one(self, req: MessagesRequest) -> None:
        self.store.archive_request(req)
        self.store.prune_if_needed()

    async def _archive_worker(self) -> None:
        while True:
            item = await self._archive_queue.get()
            try:
                if item is _STOP:
                    return
                try:
                    await asyncio.to_thread(self._archive_one, item)
                except Exception:
                    self.archive_failed += 1
            finally:
                self._archive_queue.task_done()

    async def run_once(
        self,
        request_hash: str,
        factory: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        cached = await self._read_cache(request_hash, "complete")
        if cached is not None and isinstance(cached.get("response"), dict):
            return cached["response"]

        async with self._complete_lock:
            task = self._complete_inflight.get(request_hash)
            if task is None:
                task = asyncio.create_task(
                    self._produce_complete(request_hash, factory)
                )
                self._complete_inflight[request_hash] = task
        return await asyncio.shield(task)

    async def _produce_complete(
        self,
        request_hash: str,
        factory: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        try:
            response = await factory()
            try:
                await self._write_cache(
                    request_hash,
                    "complete",
                    {"response": response},
                )
            except Exception:
                self.cache_failed += 1
            return response
        finally:
            async with self._complete_lock:
                current = self._complete_inflight.get(request_hash)
                if current is asyncio.current_task():
                    self._complete_inflight.pop(request_hash, None)

    @staticmethod
    def _terminal_event(event: str) -> bool:
        return "event: message_stop" in event

    async def stream_once(
        self,
        request_hash: str,
        factory: Callable[[], AsyncIterator[str]],
    ) -> AsyncIterator[str]:
        cached = await self._read_cache(request_hash, "stream")
        if cached is not None and isinstance(cached.get("events"), list):
            for event in cached["events"]:
                if isinstance(event, str):
                    yield event
            return

        queue: asyncio.Queue = asyncio.Queue()
        async with self._stream_lock:
            completed = self._completed_streams.get(request_hash)
            if completed is not None and completed[0] > self._now():
                completed_snapshot = completed[1]
            else:
                self._completed_streams.pop(request_hash, None)
                completed_snapshot = None
            if completed_snapshot is not None:
                shared = None
                snapshot = completed_snapshot
            else:
                shared = self._streams.get(request_hash)
                if shared is None:
                    shared = _SharedStream()
                    self._streams[request_hash] = shared
                    shared.task = asyncio.create_task(
                        self._pump_stream(request_hash, shared, factory)
                    )
                shared.subscribers.add(queue)
                snapshot = tuple(shared.events)

        try:
            for event in snapshot:
                yield event
            if shared is None:
                return
            while True:
                item = await queue.get()
                if item is _STREAM_DONE:
                    return
                if isinstance(item, _StreamError):
                    raise item.error
                yield item
        finally:
            if shared is not None:
                async with self._stream_lock:
                    shared.subscribers.discard(queue)

    async def _pump_stream(
        self,
        request_hash: str,
        shared: _SharedStream,
        factory: Callable[[], AsyncIterator[str]],
    ) -> None:
        terminal = False
        error: BaseException | None = None
        try:
            async for event in factory():
                if not isinstance(event, str):
                    raise TypeError("context stream events must be strings")
                terminal = terminal or self._terminal_event(event)
                async with self._stream_lock:
                    shared.events.append(event)
                    for subscriber in tuple(shared.subscribers):
                        subscriber.put_nowait(event)
            if terminal:
                try:
                    await self._write_cache(
                        request_hash,
                        "stream",
                        {"events": list(shared.events)},
                    )
                except Exception:
                    self.cache_failed += 1
        except BaseException as exc:
            error = exc
        finally:
            async with self._stream_lock:
                if terminal and error is None:
                    self._completed_streams[request_hash] = (
                        self._now() + self.cache_seconds,
                        tuple(shared.events),
                    )
                signal = _StreamError(error) if error is not None else _STREAM_DONE
                for subscriber in tuple(shared.subscribers):
                    subscriber.put_nowait(signal)
                if self._streams.get(request_hash) is shared:
                    self._streams.pop(request_hash, None)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        async with self._stream_lock:
            stream_tasks = [
                shared.task
                for shared in self._streams.values()
                if shared.task is not None and not shared.task.done()
            ]
        for task in stream_tasks:
            task.cancel()
        if stream_tasks:
            await asyncio.gather(*stream_tasks, return_exceptions=True)

        async with self._complete_lock:
            complete_tasks = [
                task for task in self._complete_inflight.values() if not task.done()
            ]
        for task in complete_tasks:
            task.cancel()
        if complete_tasks:
            await asyncio.gather(*complete_tasks, return_exceptions=True)

        if self._archive_task is not None:
            await self._archive_queue.put(_STOP)
            await self._archive_task
