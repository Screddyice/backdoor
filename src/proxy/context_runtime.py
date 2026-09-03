"""Asynchronous coordination for private local-context virtualization."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from dataclasses import dataclass, field
import hashlib
import time
from typing import Any

from .config import Settings
from .context_segments import ContextAdapter, NormalizedContext, canonical_json, selected_with_pairs
from .context_store import ContextStore, ContextStoreUnavailable, StoredLineage, StoredSegment
from .context_tokenizer import ContextLimitError, QwenTokenGate
from .context_window import format_historical_context, select_working_set


_STREAM_DONE = object()
_STREAM_CACHE_FIELD = "_backdoor_context_stream_events"


@dataclass(frozen=True)
class PreparedContext:
    payload: Any
    request_hash: str
    lineage_id: str | None
    token_count: int
    used_store: bool


@dataclass
class _StreamFlight:
    events: list[str] = field(default_factory=list)
    subscribers: set[asyncio.Queue[object]] = field(default_factory=set)
    task: asyncio.Task[None] | None = None


def normalized_request_hash(client_kind: str, payload: Any) -> str:
    """Hash the client kind and canonical request body without changing its order."""
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump(mode="json", exclude_none=True)
    material = canonical_json({"client_kind": client_kind, "payload": payload})
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ContextRuntime:
    """Keep archive I/O and retry sharing outside request handlers."""

    def __init__(
        self,
        store: ContextStore,
        token_gate: QwenTokenGate,
        settings: Settings | None = None,
        *,
        archive_queue_size: int = 64,
        stream_subscriber_queue_size: int = 64,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if stream_subscriber_queue_size <= 0:
            raise ValueError("stream_subscriber_queue_size must be positive")
        self.store = store
        self.token_gate = token_gate
        self.settings = settings or Settings(_env_file=None)
        self._clock = clock
        self._archive_queue: asyncio.Queue[NormalizedContext] = asyncio.Queue(
            maxsize=archive_queue_size
        )
        self._archive_task: asyncio.Task[None] | None = None
        self._complete_flights: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._stream_flights: dict[str, _StreamFlight] = {}
        self._stream_locks: dict[str, asyncio.Lock] = {}
        self._stream_subscriber_queue_size = stream_subscriber_queue_size
        self.archive_dropped = 0
        self._closed = False

    def archive_cloud(self, context: NormalizedContext) -> None:
        """Queue a cloud transcript for private archival without delaying the response."""
        if self._closed:
            return
        try:
            self._archive_queue.put_nowait(context)
        except asyncio.QueueFull:
            self.archive_dropped += 1
            return
        self._start_archive_worker()

    async def prepare_local(
        self,
        context: NormalizedContext,
        adapter: ContextAdapter,
        settings: Settings,
    ) -> PreparedContext:
        """Build one hard-gated local payload, retaining a stateless fallback path."""
        request_hash = normalized_request_hash(context.client_kind, context.native)
        try:
            async with asyncio.timeout(settings.context_archive_timeout_seconds):
                lineage = await asyncio.to_thread(self.store.archive, context)
            try:
                async with asyncio.timeout(settings.context_archive_timeout_seconds):
                    await asyncio.to_thread(self.store.prune_if_needed, self._clock())
            except (ContextStoreUnavailable, TimeoutError):
                pass
            async with asyncio.timeout(settings.context_assembly_timeout_seconds):
                selected_ids, retrieved = await asyncio.to_thread(
                    self._select_from_store, context, adapter, lineage, settings
                )
                payload, token_count = await asyncio.to_thread(
                    self._rebuild_and_require_fit,
                    context,
                    adapter,
                    selected_ids,
                    retrieved,
                    settings,
                )
        except (ContextStoreUnavailable, TimeoutError):
            selected_ids = await asyncio.to_thread(
                self._newest_tail, context, adapter, settings
            )
            payload, token_count = await asyncio.to_thread(
                self._rebuild_and_require_fit,
                context,
                adapter,
                selected_ids,
                (),
                settings,
            )
            return PreparedContext(payload, request_hash, None, token_count, False)
        return PreparedContext(payload, request_hash, lineage.lineage_id, token_count, True)

    async def run_complete_once(
        self,
        key: str,
        factory: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Share one non-stream generation and persist only a completed result."""
        task = self._complete_flights.get(key)
        if task is None:
            task = asyncio.create_task(self._complete(key, factory))
            self._complete_flights[key] = task
            task.add_done_callback(lambda done: self._complete_done(key, done))
        return await asyncio.shield(task)

    async def stream_once(
        self,
        key: str,
        factory: Callable[[], AsyncIterator[str]],
    ) -> AsyncIterator[str]:
        """Share a live stream and replay completed streams only after their terminal event."""
        cached: list[str] | None = None
        queue: asyncio.Queue[object] | None = None
        async with self._stream_lock(key):
            flight = self._stream_flights.get(key)
            if flight is None:
                cached = await self._cached_stream(key)
                if cached is None:
                    flight = _StreamFlight()
                    self._stream_flights[key] = flight
            if flight is not None:
                queue = asyncio.Queue(maxsize=self._stream_subscriber_queue_size)
                for event in flight.events:
                    self._publish_to_subscriber(flight, queue, event)
                flight.subscribers.add(queue)
                if flight.task is None:
                    flight.task = asyncio.create_task(self._produce_stream(key, flight, factory))
                    flight.task.add_done_callback(self._consume_task_result)

        if cached is not None:
            for event in cached:
                yield event
            return

        if flight is None or queue is None:  # Defensive: either a cache or a flight exists above.
            raise RuntimeError("stream runtime could not establish a replay source")

        try:
            while True:
                item = await queue.get()
                if item is _STREAM_DONE:
                    return
                if isinstance(item, BaseException):
                    raise item
                yield str(item)
        finally:
            flight.subscribers.discard(queue)

    def close(self) -> None:
        """Cancel private background work without blocking caller shutdown."""
        self._closed = True
        for task in (
            self._archive_task,
            *self._complete_flights.values(),
            *(flight.task for flight in self._stream_flights.values()),
        ):
            if task is not None:
                task.cancel()
        self._complete_flights.clear()

    def _start_archive_worker(self) -> None:
        if self._archive_task is not None and not self._archive_task.done():
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._archive_task = asyncio.create_task(self._archive_worker())

    async def _archive_worker(self) -> None:
        while not self._closed:
            context = await self._archive_queue.get()
            try:
                async with asyncio.timeout(self.settings.context_archive_timeout_seconds):
                    await asyncio.to_thread(self.store.archive, context)
                    await asyncio.to_thread(self.store.prune_if_needed, self._clock())
            except (ContextStoreUnavailable, TimeoutError):
                pass
            finally:
                self._archive_queue.task_done()

    def _select_from_store(
        self,
        context: NormalizedContext,
        adapter: ContextAdapter,
        lineage: StoredLineage,
        settings: Settings,
    ) -> tuple[tuple[str, ...], tuple[StoredSegment, ...]]:
        def count(selected_ids: Collection[str], retrieved: Collection[StoredSegment]) -> int:
            payload = adapter.rebuild(
                context, selected_ids, format_historical_context(retrieved)
            )
            return self.token_gate.count(self._provider_payload(payload)).tokens

        selection = select_working_set(
            context,
            self.store,
            lineage,
            settings.context_target_input_tokens,
            settings.context_hard_input_tokens,
            count,
        )
        if selection.reason is not None:
            raise ContextLimitError(selection.reason)
        return selection.selected_ids, selection.retrieved

    def _newest_tail(
        self,
        context: NormalizedContext,
        adapter: ContextAdapter,
        settings: Settings,
    ) -> tuple[str, ...]:
        current = next(
            segment for segment in context.segments if segment.segment_id == context.current_segment_id
        )
        selected = selected_with_pairs(context.segments, {context.current_segment_id})
        for segment in context.segments:
            if segment.ordinal <= current.ordinal or segment.pair_id is None:
                continue
            candidate = selected_with_pairs(context.segments, selected | {segment.segment_id})
            payload = adapter.rebuild(context, candidate)
            if (
                self.token_gate.count(self._provider_payload(payload)).tokens
                > settings.context_hard_input_tokens
            ):
                raise ContextLimitError("active_pair_over_limit")
            selected = candidate
        for segment in reversed(context.segments):
            if segment.segment_id in selected:
                continue
            candidate = selected_with_pairs(
                context.segments, selected | {segment.segment_id}
            )
            payload = adapter.rebuild(context, candidate)
            if (
                self.token_gate.count(self._provider_payload(payload)).tokens
                <= settings.context_target_input_tokens
            ):
                selected = candidate
        return tuple(
            segment.segment_id for segment in context.segments if segment.segment_id in selected
        )

    def _rebuild_and_require_fit(
        self,
        context: NormalizedContext,
        adapter: ContextAdapter,
        selected_ids: Collection[str],
        retrieved: Collection[StoredSegment],
        settings: Settings,
    ) -> tuple[Any, int]:
        payload = adapter.rebuild(
            context, selected_ids, format_historical_context(retrieved)
        )
        result = self.token_gate.require_fit(
            self._provider_payload(payload), settings.context_hard_input_tokens
        )
        return payload, result.tokens

    async def _complete(
        self,
        key: str,
        factory: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        cached = await self._cached_response(key)
        if cached is not None:
            return cached
        response = await factory()
        if self.settings.context_response_cache_seconds:
            try:
                await asyncio.to_thread(
                    self.store.put_cached_response,
                    key,
                    response,
                    self._clock() + self.settings.context_response_cache_seconds,
                )
            except ContextStoreUnavailable:
                pass
        return response

    def _complete_done(self, key: str, task: asyncio.Task[dict[str, Any]]) -> None:
        if self._complete_flights.get(key) is task:
            self._complete_flights.pop(key, None)

    async def _cached_response(self, key: str) -> dict[str, Any] | None:
        if not self.settings.context_response_cache_seconds:
            return None
        try:
            return await asyncio.to_thread(self.store.get_cached_response, key, self._clock())
        except ContextStoreUnavailable:
            return None

    async def _cached_stream(self, key: str) -> list[str] | None:
        cached = await self._cached_response(f"stream:{key}")
        if cached is None:
            return None
        events = cached.get(_STREAM_CACHE_FIELD)
        if not isinstance(events, list) or not all(isinstance(event, str) for event in events):
            return None
        return events

    async def _produce_stream(
        self,
        key: str,
        flight: _StreamFlight,
        factory: Callable[[], AsyncIterator[str]],
    ) -> None:
        terminal: object = _STREAM_DONE
        try:
            async for event in factory():
                await self._publish_stream_event(key, flight, event)
            await self._store_stream(key, flight.events)
        except BaseException as exc:
            terminal = exc
            raise
        finally:
            await asyncio.shield(self._finish_stream(key, flight, terminal))

    async def _store_stream(self, key: str, events: list[str]) -> None:
        if not self.settings.context_response_cache_seconds:
            return
        try:
            await asyncio.to_thread(
                self.store.put_cached_response,
                f"stream:{key}",
                {_STREAM_CACHE_FIELD: events},
                self._clock() + self.settings.context_response_cache_seconds,
            )
        except ContextStoreUnavailable:
            pass

    async def _publish_stream_event(
        self, key: str, flight: _StreamFlight, event: str
    ) -> None:
        async with self._stream_lock(key):
            if self._stream_flights.get(key) is not flight:
                return
            flight.events.append(event)
            for queue in tuple(flight.subscribers):
                self._publish_to_subscriber(flight, queue, event)

    async def _finish_stream(
        self, key: str, flight: _StreamFlight, terminal: object
    ) -> None:
        async with self._stream_lock(key):
            if self._stream_flights.get(key) is not flight:
                return
            for queue in tuple(flight.subscribers):
                self._publish_to_subscriber(flight, queue, terminal)
            self._stream_flights.pop(key, None)

    @staticmethod
    def _consume_task_result(task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except (asyncio.CancelledError, Exception):
            pass

    def _stream_lock(self, key: str) -> asyncio.Lock:
        lock = self._stream_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._stream_locks[key] = lock
        return lock

    @staticmethod
    def _publish_to_subscriber(
        flight: _StreamFlight, queue: asyncio.Queue[object], item: object
    ) -> None:
        if queue.full():
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(RuntimeError("stream subscriber fell behind"))
            flight.subscribers.discard(queue)
            return
        queue.put_nowait(item)

    @staticmethod
    def _provider_payload(payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            return payload
        if hasattr(payload, "model_dump"):
            return payload.model_dump(mode="json", exclude_none=True)
        raise TypeError("context adapter rebuilt a non-serializable payload")
