"""Codex Responses relay with an independent cloud-to-local breaker."""

from __future__ import annotations

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
    enforce_cloud_budget,
    extract_recall_query,
)
from .cognee_recall import recall_context
from .config import Settings, get_settings
from .failover import FailoverBreaker
from . import mlx_admin, ollama_admin

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

_chatgpt_client: httpx.AsyncClient | None = None
_ollama_client: httpx.AsyncClient | None = None
_codex_breaker: FailoverBreaker | None = None
_local_inflight = 0
_deferred_claims: set[tuple[str, str]] = set()


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


async def _send_cloud(request: Request, body: bytes, settings: Settings) -> httpx.Response:
    headers = {key: value for key, value in request.headers.items() if key.lower() not in _HOP_HEADERS}
    headers.setdefault("accept-encoding", "identity")
    upstream = _get_chatgpt_client(settings)
    upstream_request = upstream.build_request(
        request.method,
        "responses",
        content=body,
        headers=headers,
    )
    return await upstream.send(upstream_request, stream=True)


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


def _relay_cloud(response: httpx.Response, breaker: FailoverBreaker) -> StreamingResponse:
    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in _SKIP_RESPONSE_HEADERS
    }
    return StreamingResponse(
        _cloud_body(response, breaker),
        status_code=response.status_code,
        headers=headers,
        background=BackgroundTask(response.aclose),
    )


async def _release_claims(
    breaker: FailoverBreaker, settings: Settings
) -> None:
    global _deferred_claims
    claims = breaker.drain_claims()
    if not claims:
        return
    if _local_inflight:
        _deferred_claims.update(claims)
        return
    for base_url, model in claims:
        await ollama_admin.unload(base_url, model)


async def _local_body(
    response: httpx.Response,
    breaker: FailoverBreaker,
) -> AsyncIterator[bytes]:
    global _local_inflight, _deferred_claims
    _local_inflight += 1
    try:
        async for chunk in response.aiter_raw():
            yield chunk
    finally:
        _local_inflight = max(0, _local_inflight - 1)
        await response.aclose()
        if not _local_inflight and _deferred_claims and not breaker.open:
            claims, _deferred_claims = _deferred_claims, set()
            for base_url, model in claims:
                await ollama_admin.unload(base_url, model)


async def _serve_local(
    cloud_payload: dict,
    settings: Settings,
    breaker: FailoverBreaker,
    correlation_id: str,
    started: float,
) -> StreamingResponse:
    if not settings.codex_failover_to_local:
        raise HTTPException(status_code=502, detail="ChatGPT Codex unavailable")

    resolved = await mlx_admin.resolve_profile("local-qwen38-obliterated")
    if resolved != "local-qwen38-obliterated":
        logger.warning(
            "Codex local route id=%s exact Qwen runtime unavailable",
            correlation_id,
        )
        raise HTTPException(status_code=503, detail="Local Qwen runtime unavailable")

    try:
        query = extract_recall_query(cloud_payload)
        memories = await recall_context(query, settings)
        local_payload, budget = build_local_payload(cloud_payload, memories, settings)
    except CodexRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

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
        logger.warning("Codex local request failed id=%s status=%d", correlation_id, status)
        raise HTTPException(status_code=502, detail="Local Qwen rejected the request")
    breaker.note_claim(_ollama_openai_base(settings), settings.codex_local_model)
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
    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in _SKIP_RESPONSE_HEADERS
    }
    return StreamingResponse(
        _local_body(response, breaker),
        status_code=response.status_code,
        headers=headers,
    )


@codex_router.post("/responses")
async def codex_responses(
    request: Request,
    settings: Settings = Depends(get_settings),
):
    started = time.monotonic()
    correlation_id = uuid.uuid4().hex[:12]
    body = await request.body()
    try:
        payload = decode_codex_body(body, request.headers.get("content-encoding", ""))
        max_input = settings.codex_context_window - settings.codex_reply_reserve_tokens
        estimated = enforce_cloud_budget(payload, max_input)
    except CodexRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    breaker = get_codex_breaker(settings)
    if not breaker.allow_upstream():
        return await _serve_local(payload, settings, breaker, correlation_id, started)

    try:
        response = await _send_cloud(request, body, settings)
    except httpx.TransportError as exc:
        logger.warning(
            "Codex cloud request failed id=%s error=%s",
            correlation_id,
            type(exc).__name__,
        )
        if breaker.record_failure(type(exc).__name__):
            return await _serve_local(payload, settings, breaker, correlation_id, started)
        raise HTTPException(status_code=502, detail="ChatGPT Codex unavailable") from exc

    if response.status_code in _failover_statuses(settings):
        decoded = await response.aread()
        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in _DECODED_SKIP_RESPONSE_HEADERS
        }
        await response.aclose()
        if breaker.record_failure(f"HTTP {response.status_code}"):
            return await _serve_local(payload, settings, breaker, correlation_id, started)
        return Response(content=decoded, status_code=response.status_code, headers=headers)

    was_open = breaker.open
    breaker.record_success()
    if was_open:
        await _release_claims(breaker, settings)
    logger.info(
        "Codex route id=%s path=cloud status=%d input_tokens=%d elapsed_ms=%d",
        correlation_id,
        response.status_code,
        estimated,
        round((time.monotonic() - started) * 1000),
    )
    return _relay_cloud(response, breaker)
