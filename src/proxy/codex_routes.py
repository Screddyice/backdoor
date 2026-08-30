"""Codex Responses relay with an independent cloud-to-local breaker."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import AsyncIterator
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from .codex_context import (
    CodexRequestError,
    build_local_payload,
    decode_codex_body,
    extract_recall_query,
)
from .external_context import (
    prepare_codex_external_context,
    recall_codex_external_context,
)
from .cognee_recall import recall_context
from .config import Settings, get_settings
from .failover import FailoverBreaker
from . import compute_lease, mlx_admin, ollama_admin

logger = logging.getLogger(__name__)
codex_router = APIRouter(prefix="/backend-api/codex")

_HOP_HEADERS = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "upgrade",
    "proxy-authorization",
    "proxy-authenticate",
}
_SKIP_RESPONSE_HEADERS = {"content-length", "transfer-encoding", "connection"}
_DECODED_SKIP_RESPONSE_HEADERS = _SKIP_RESPONSE_HEADERS | {"content-encoding"}
_MAX_LOCAL_SSE_FRAME_BYTES = 8 * 1024 * 1024
_MAX_LOCAL_JSON_BODY_BYTES = 8 * 1024 * 1024

_chatgpt_client: httpx.AsyncClient | None = None
_ollama_client: httpx.AsyncClient | None = None
_codex_breaker: FailoverBreaker | None = None
_local_inflight = 0
_deferred_claims: set[tuple[str, str]] = set()
_local_state_lock = asyncio.Lock()


def _new_chatgpt_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.codex_chatgpt_upstream.rstrip("/") + "/",
        timeout=httpx.Timeout(connect=30.0, read=600.0, write=60.0, pool=5.0),
    )


def _get_chatgpt_client(settings: Settings) -> httpx.AsyncClient:
    global _chatgpt_client
    if _chatgpt_client is None:
        _chatgpt_client = _new_chatgpt_client(settings)
    return _chatgpt_client


def _get_ollama_client() -> httpx.AsyncClient:
    global _ollama_client
    if _ollama_client is None:
        _ollama_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30.0, read=600.0, write=60.0, pool=5.0)
        )
    return _ollama_client


def get_codex_breaker(settings: Settings) -> FailoverBreaker:
    global _codex_breaker
    if _codex_breaker is None:
        _codex_breaker = FailoverBreaker(
            threshold=settings.codex_failover_threshold,
            window=settings.codex_failover_window_seconds,
            probe_interval=settings.codex_failover_probe_seconds,
            source="codex",
            upstream_name="ChatGPT Codex",
            require_offline=settings.codex_failover_require_offline,
        )
    return _codex_breaker


def _failover_statuses(settings: Settings) -> set[int]:
    excluded = {400, 401, 403}
    return {
        int(part.strip())
        for part in settings.codex_failover_statuses.split(",")
        if part.strip().isdigit() and int(part.strip()) not in excluded
    }


async def close_codex_clients() -> None:
    global _chatgpt_client, _ollama_client
    for client in (_chatgpt_client, _ollama_client):
        if client is not None:
            await client.aclose()
    _chatgpt_client = None
    _ollama_client = None


async def _send_cloud(
    request: Request,
    body: bytes,
    settings: Settings,
    path: str = "responses",
) -> httpx.Response:
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _HOP_HEADERS
    }
    headers.setdefault("accept-encoding", "identity")
    upstream = _get_chatgpt_client(settings)
    upstream_request = upstream.build_request(
        request.method,
        path,
        content=body,
        headers=headers,
    )
    return await upstream.send(upstream_request, stream=True)


async def _read_bounded_body(request: Request, max_bytes: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > max_bytes:
        raise HTTPException(
            status_code=413, detail="Codex request exceeds the encoded size limit"
        )
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            raise HTTPException(
                status_code=413, detail="Codex request exceeds the encoded size limit"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _decode_local_payload(body: bytes, request: Request, settings: Settings) -> dict:
    try:
        return decode_codex_body(
            body,
            request.headers.get("content-encoding", ""),
            max_decoded_bytes=settings.codex_max_request_bytes,
        )
    except CodexRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@codex_router.get("/models")
async def codex_models(
    request: Request,
    settings: Settings = Depends(get_settings),
):
    """Relay the provider probe used by Codex CLI and Desktop diagnostics."""
    try:
        response = await _send_cloud(request, b"", settings, path="models")
        body = await response.aread()
        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in _DECODED_SKIP_RESPONSE_HEADERS
        }
        status = response.status_code
        await response.aclose()
        return Response(content=body, status_code=status, headers=headers)
    except httpx.TransportError as exc:
        raise HTTPException(status_code=502, detail="ChatGPT Codex unavailable") from exc


async def _send_local(payload: dict, settings: Settings) -> httpx.Response:
    client = _get_ollama_client()
    local_request = client.build_request(
        "POST",
        settings.codex_local_responses_url,
        json=payload,
        headers={
            "content-type": "application/json",
            "accept": "text/event-stream",
        },
    )
    return await client.send(local_request, stream=True)


def _ollama_openai_base(settings: Settings) -> str:
    parsed = urlsplit(settings.codex_local_responses_url)
    return f"{parsed.scheme}://{parsed.netloc}/v1"


async def _cloud_body(
    response: httpx.Response, breaker: FailoverBreaker
) -> AsyncIterator[bytes]:
    try:
        async for chunk in response.aiter_raw():
            yield chunk
    except httpx.TransportError as exc:
        logger.warning(
            "Codex cloud stream failed after %d byte(s) (%s)",
            response.num_bytes_downloaded,
            type(exc).__name__,
        )
        breaker.record_failure(type(exc).__name__)
        raise
    else:
        # Reachability is NOT credited here — `codex_route` already did that the
        # moment the response headers arrived. What is deliberately held back to
        # this point is releasing the local tier, because the two questions have
        # different answers:
        #
        #   "Is ChatGPT reachable?"      answered by the headers.
        #   "Is the qwen tier spare?"    answered only by a stream that finished.
        #
        # A probe whose stream dies mid-flight is about to be retried, and
        # unloading the tier that will serve that retry only buys a reload — see
        # test_half_open_stream_failure_keeps_breaker_open_and_qwen_claimed.
        #
        # `not breaker.open` rather than a captured `was_open`: by the time this
        # runs the breaker is normally already closed (headers did it), so the
        # question that matters is whether it is closed NOW, not whether this
        # particular response is the one that closed it.
        if not breaker.open:
            await _release_claims(breaker)


async def _passthrough_body(response: httpx.Response) -> AsyncIterator[bytes]:
    async for chunk in response.aiter_raw():
        yield chunk


class _ManagedStreamingResponse(StreamingResponse):
    """Always close the iterator and upstream response on client disconnect."""

    async def __call__(self, scope, receive, send) -> None:
        background, self.background = self.background, None
        try:
            await super().__call__(scope, receive, send)
        finally:
            close_iterator = getattr(self.body_iterator, "aclose", None)
            try:
                if close_iterator is not None:
                    await close_iterator()
            finally:
                if background is not None:
                    await background()


def _relay_passthrough(response: httpx.Response) -> StreamingResponse:
    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in _SKIP_RESPONSE_HEADERS
    }
    return _ManagedStreamingResponse(
        _passthrough_body(response),
        status_code=response.status_code,
        headers=headers,
        background=BackgroundTask(response.aclose),
    )


def _relay_cloud(response: httpx.Response, breaker: FailoverBreaker) -> StreamingResponse:
    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in _SKIP_RESPONSE_HEADERS
    }
    return _ManagedStreamingResponse(
        _cloud_body(response, breaker),
        status_code=response.status_code,
        headers=headers,
        background=BackgroundTask(response.aclose),
    )


async def _release_claims(breaker: FailoverBreaker) -> None:
    global _deferred_claims
    async with _local_state_lock:
        claims = breaker.drain_claims()
        if _local_inflight:
            _deferred_claims.update(claims)
            return
        if not breaker.open and _deferred_claims:
            claims.update(_deferred_claims)
            _deferred_claims = set()
        for base_url, model in claims:
            await ollama_admin.unload(base_url, model)


def _sanitize_local_sse_frame(
    frame: bytes,
    dropped_indices: set[int],
    dropped_item_ids: set[str],
) -> bytes:
    lines = frame.rstrip(b"\r\n").splitlines()
    data_lines = [line[5:].lstrip() for line in lines if line.startswith(b"data:")]
    if not data_lines or data_lines == [b"[DONE]"]:
        return frame
    try:
        payload = json.loads(b"\n".join(data_lines))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        logger.warning("Codex local route received an unparseable SSE frame")
        raise ValueError("Codex local route received an unparseable SSE frame") from exc
    if not isinstance(payload, dict):
        return frame

    event_type = str(payload.get("type") or "")
    output_index = payload.get("output_index")
    item = payload.get("item")
    item_id = payload.get("item_id")
    part = payload.get("part")
    reasoning_event = event_type.startswith("response.reasoning")
    reasoning_item = isinstance(item, dict) and item.get("type") == "reasoning"
    reasoning_part = (
        isinstance(part, dict) and str(part.get("type") or "").startswith("reasoning")
    )
    if (
        reasoning_event
        or reasoning_item
        or isinstance(item_id, str)
        and item_id in dropped_item_ids
        or reasoning_part
    ):
        if isinstance(output_index, int):
            dropped_indices.add(output_index)
        if reasoning_item and isinstance(item.get("id"), str):
            dropped_item_ids.add(item["id"])
        if (reasoning_event or reasoning_part) and isinstance(item_id, str):
            dropped_item_ids.add(item_id)
        return b""

    changed = False
    response = payload.get("response")
    if isinstance(response, dict) and isinstance(response.get("output"), list):
        output = response["output"]
        filtered = [
            value
            for value in output
            if not isinstance(value, dict) or value.get("type") != "reasoning"
        ]
        if len(filtered) != len(output):
            response["output"] = filtered
            changed = True

    if isinstance(output_index, int):
        adjusted_index = output_index - sum(
            dropped_index < output_index for dropped_index in dropped_indices
        )
        if adjusted_index != output_index:
            payload["output_index"] = adjusted_index
            changed = True

    if not changed:
        return frame

    newline = b"\r\n" if b"\r\n" in frame else b"\n"
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    rebuilt: list[bytes] = []
    wrote_data = False
    for line in lines:
        if line.startswith(b"data:"):
            if not wrote_data:
                rebuilt.append(b"data: " + encoded)
                wrote_data = True
            continue
        rebuilt.append(line)
    return newline.join(rebuilt) + newline + newline


def _sanitize_local_json_value(value):
    if isinstance(value, list):
        sanitized = []
        for child in value:
            if isinstance(child, dict):
                child_type = str(child.get("type") or "")
                if child_type == "reasoning" or child_type.startswith("reasoning_"):
                    continue
            sanitized.append(_sanitize_local_json_value(child))
        return sanitized
    if isinstance(value, dict):
        return {
            key: _sanitize_local_json_value(child)
            for key, child in value.items()
            if key != "encrypted_content"
        }
    return value


async def _read_local_json_response(response: httpx.Response) -> dict:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > _MAX_LOCAL_JSON_BODY_BYTES:
            raise ValueError("Codex local JSON response exceeded the size limit")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError("Codex local route received invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Codex local route received a non-object JSON response")
    return _sanitize_local_json_value(payload)


class _LocalBody(AsyncIterator[bytes]):
    def __init__(self, response: httpx.Response, breaker: FailoverBreaker):
        self._response = response
        self._breaker = breaker
        self._iterator = response.aiter_raw().__aiter__()
        self._buffer = bytearray()
        self._scan_from = 0
        self._dropped_indices: set[int] = set()
        self._dropped_item_ids: set[str] = set()
        self._closed = False

    def __aiter__(self) -> "_LocalBody":
        return self

    async def __anext__(self) -> bytes:
        if self._closed:
            raise StopAsyncIteration
        while True:
            frames: list[bytes] = []
            while True:
                separators = [
                    (index, length)
                    for index, length in (
                        (self._buffer.find(b"\n\n", self._scan_from), 2),
                        (self._buffer.find(b"\r\n\r\n", self._scan_from), 4),
                    )
                    if index >= 0
                ]
                if not separators:
                    if len(self._buffer) > _MAX_LOCAL_SSE_FRAME_BYTES:
                        logger.warning(
                            "Codex local route exceeded the %d-byte SSE frame limit",
                            _MAX_LOCAL_SSE_FRAME_BYTES,
                        )
                        await self.aclose()
                        raise ValueError("Codex local SSE frame exceeded the size limit")
                    self._scan_from = max(0, len(self._buffer) - 3)
                    break
                frame_index, separator_length = min(separators)
                frame_end = frame_index + separator_length
                if frame_end > _MAX_LOCAL_SSE_FRAME_BYTES:
                    logger.warning(
                        "Codex local route exceeded the %d-byte SSE frame limit",
                        _MAX_LOCAL_SSE_FRAME_BYTES,
                    )
                    await self.aclose()
                    raise ValueError("Codex local SSE frame exceeded the size limit")
                frame = bytes(self._buffer[:frame_end])
                del self._buffer[:frame_end]
                self._scan_from = 0
                try:
                    sanitized = _sanitize_local_sse_frame(
                        frame,
                        self._dropped_indices,
                        self._dropped_item_ids,
                    )
                except BaseException:
                    await self.aclose()
                    raise
                if sanitized:
                    frames.append(sanitized)
            if frames:
                return b"".join(frames)
            try:
                self._buffer.extend(await self._iterator.__anext__())
            except StopAsyncIteration:
                if self._buffer:
                    logger.warning(
                        "Codex local route discarded an incomplete SSE frame"
                    )
                    self._buffer = b""
                await self.aclose()
                raise
            except BaseException:
                await self.aclose()
                raise

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        close_iterator = getattr(self._iterator, "aclose", None)
        try:
            if close_iterator is not None:
                await close_iterator()
        finally:
            try:
                await self._response.aclose()
            finally:
                await _release_local_slot(self._breaker)


def _local_body(
    response: httpx.Response,
    breaker: FailoverBreaker,
    *,
    reserved: bool = False,
) -> _LocalBody:
    global _local_inflight
    if not reserved:
        _local_inflight += 1
    return _LocalBody(response, breaker)


async def _release_local_slot(breaker: FailoverBreaker) -> None:
    global _local_inflight, _deferred_claims
    async with _local_state_lock:
        _local_inflight = max(0, _local_inflight - 1)
        if not _local_inflight and _deferred_claims and not breaker.open:
            claims, _deferred_claims = _deferred_claims, set()
            for base_url, model in claims:
                await ollama_admin.unload(base_url, model)


async def _reserve_local_slot() -> None:
    global _local_inflight
    async with _local_state_lock:
        _local_inflight += 1


async def _serve_local(
    cloud_payload: dict,
    settings: Settings,
    breaker: FailoverBreaker,
    correlation_id: str,
    started: float,
) -> Response:
    global _local_inflight, _deferred_claims
    if not settings.codex_failover_to_local:
        raise HTTPException(status_code=502, detail="ChatGPT Codex unavailable")

    await _reserve_local_slot()
    response: httpx.Response | None = None
    try:
        resolved = await mlx_admin.resolve_profile("local-qwen38-obliterated")
        if resolved != "local-qwen38-obliterated":
            logger.warning(
                "Codex local route id=%s exact Qwen runtime unavailable",
                correlation_id,
            )
            raise HTTPException(
                status_code=503, detail="Local Qwen runtime unavailable"
            )

        breaker.note_claim(_ollama_openai_base(settings), settings.codex_local_model)
        if not breaker.open:
            _deferred_claims.update(breaker.drain_claims())

        try:
            query = extract_recall_query(
                cloud_payload, settings.codex_active_turn_budget_tokens
            )
            local_source = await prepare_codex_external_context(cloud_payload, settings)
            if settings.qwen_cognee:
                external_memories, agent_memories = await asyncio.gather(
                    recall_codex_external_context(cloud_payload, settings),
                    recall_context(query, settings),
                )
                memories = agent_memories + external_memories
            else:
                memories = []
            local_payload, budget = build_local_payload(
                local_source, memories, settings
            )
        except CodexRequestError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

        compute_lease.claim_exclusive_model(
            settings.codex_local_model,
            source="codex-failover",
            ttl_seconds=600,
        )
        try:
            response = await _send_local(local_payload, settings)
        except httpx.TransportError as exc:
            logger.warning(
                "Codex local request failed id=%s error=%s",
                correlation_id,
                type(exc).__name__,
            )
            raise HTTPException(status_code=502, detail="Local Qwen unavailable") from exc
        if response.status_code >= 400:
            status = response.status_code
            await response.aclose()
            response = None
            logger.warning(
                "Codex local request failed id=%s status=%d", correlation_id, status
            )
            raise HTTPException(
                status_code=502, detail="Local Qwen rejected the request"
            )
        logger.info(
            "Codex route id=%s path=local model=%s input_tokens=%d memories=%d "
            "tools=%d dropped_tools=%d elapsed_ms=%d",
            correlation_id,
            settings.codex_local_model,
            budget.input_tokens,
            len(memories),
            len(local_payload.get("tools", [])),
            budget.dropped_tools,
            round((time.monotonic() - started) * 1000),
        )
        if not local_payload["stream"]:
            try:
                local_json = await _read_local_json_response(response)
            except httpx.TransportError as exc:
                logger.warning(
                    "Codex local response failed id=%s error=%s",
                    correlation_id,
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=502, detail="Local Qwen unavailable"
                ) from exc
            except ValueError as exc:
                raise HTTPException(
                    status_code=502, detail="Local Qwen returned an invalid response"
                ) from exc
            headers = {
                key: value
                for key, value in response.headers.items()
                if key.lower() not in _DECODED_SKIP_RESPONSE_HEADERS
            }
            content = json.dumps(local_json, separators=(",", ":")).encode()
            status_code = response.status_code
            await response.aclose()
            response = None
            await _release_local_slot(breaker)
            return Response(
                content=content,
                status_code=status_code,
                headers=headers,
                media_type="application/json",
            )
        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in _SKIP_RESPONSE_HEADERS
        }
        stream = _local_body(response, breaker, reserved=True)
        return _ManagedStreamingResponse(
            stream,
            status_code=response.status_code,
            headers=headers,
            background=BackgroundTask(stream.aclose),
        )
    except BaseException:
        if response is not None:
            await response.aclose()
        await _release_local_slot(breaker)
        raise


@codex_router.post("/responses/compact")
async def codex_compact(
    request: Request,
    settings: Settings = Depends(get_settings),
):
    """Relay Codex cloud compaction without involving the inference breaker."""
    body = await _read_bounded_body(request, settings.codex_max_request_bytes)
    path = "responses/compact"
    if request.url.query:
        path = f"{path}?{request.url.query}"
    try:
        response = await _send_cloud(request, body, settings, path=path)
    except httpx.TransportError as exc:
        raise HTTPException(status_code=502, detail="ChatGPT Codex unavailable") from exc
    return _relay_passthrough(response)


@codex_router.post("/responses")
async def codex_responses(
    request: Request,
    settings: Settings = Depends(get_settings),
):
    started = time.monotonic()
    correlation_id = uuid.uuid4().hex[:12]
    body = await _read_bounded_body(request, settings.codex_max_request_bytes)

    breaker = get_codex_breaker(settings)
    failover_enabled = settings.codex_failover_to_local
    if failover_enabled and not breaker.allow_upstream():
        payload = _decode_local_payload(body, request, settings)
        return await _serve_local(payload, settings, breaker, correlation_id, started)

    try:
        response = await _send_cloud(request, body, settings)
    except httpx.TransportError as exc:
        logger.warning(
            "Codex cloud request failed id=%s error=%s",
            correlation_id,
            type(exc).__name__,
        )
        if failover_enabled and breaker.record_failure(type(exc).__name__):
            payload = _decode_local_payload(body, request, settings)
            return await _serve_local(payload, settings, breaker, correlation_id, started)
        raise HTTPException(status_code=502, detail="ChatGPT Codex unavailable") from exc

    if failover_enabled and response.status_code in _failover_statuses(settings):
        decoded = await response.aread()
        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in _DECODED_SKIP_RESPONSE_HEADERS
        }
        await response.aclose()
        if breaker.record_failure(f"HTTP {response.status_code}"):
            payload = _decode_local_payload(body, request, settings)
            return await _serve_local(payload, settings, breaker, correlation_id, started)
        return Response(content=decoded, status_code=response.status_code, headers=headers)

    logger.info(
        "Codex route id=%s path=cloud status=%d request_bytes=%d elapsed_ms=%d",
        correlation_id,
        response.status_code,
        len(body),
        round((time.monotonic() - started) * 1000),
    )
    if not failover_enabled:
        return _relay_passthrough(response)
    # Credit reachability HERE, on the headers, exactly as the Anthropic path
    # does (see routes.py `_try_upstream`) — not at the end of the relayed
    # stream.
    #
    # Recording it at the end of the stream is what pinned Codex to a local 27B
    # for 19 minutes on 2026-08-30. A blip opened the breaker at 23:09:48; the
    # half-open probes at 23:13:46 and 23:15:23 both reached ChatGPT and logged
    # `path=cloud status=200`, and the breaker still never closed — because the
    # client hung up before the body finished, and a client disconnect closes
    # this generator with GeneratorExit at the `yield`. That runs NEITHER the
    # `except httpx.TransportError` branch nor the `else`, so a demonstrably
    # successful probe was silently discarded. Worse, a client that hangs up
    # before the first chunk leaves the generator never started at all, so no
    # amount of care inside the body can see that probe succeed.
    #
    # Headers are the right place on the merits too, not just for reachability:
    # the breaker's whole question is "can this host still talk to ChatGPT", and
    # a status line is proof that it can. What the body is still needed for is
    # the tier release — see `_cloud_body`.
    breaker.record_success()
    return _relay_cloud(response, breaker)
