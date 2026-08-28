"""Codex Responses relay with an independent cloud-to-local breaker."""

from __future__ import annotations

import logging
import time
import uuid
from typing import AsyncIterator

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from .codex_context import CodexRequestError, decode_codex_body, enforce_cloud_budget
from .config import Settings, get_settings
from .failover import FailoverBreaker

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
        raise HTTPException(status_code=502, detail="ChatGPT Codex unavailable")

    try:
        response = await _send_cloud(request, body, settings)
    except httpx.TransportError as exc:
        logger.warning(
            "Codex cloud request failed id=%s error=%s",
            correlation_id,
            type(exc).__name__,
        )
        breaker.record_failure(type(exc).__name__)
        raise HTTPException(status_code=502, detail="ChatGPT Codex unavailable") from exc

    if response.status_code in _failover_statuses(settings):
        decoded = await response.aread()
        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in _DECODED_SKIP_RESPONSE_HEADERS
        }
        await response.aclose()
        breaker.record_failure(f"HTTP {response.status_code}")
        return Response(content=decoded, status_code=response.status_code, headers=headers)

    breaker.record_success()
    logger.info(
        "Codex route id=%s path=cloud status=%d input_tokens=%d elapsed_ms=%d",
        correlation_id,
        response.status_code,
        estimated,
        round((time.monotonic() - started) * 1000),
    )
    return _relay_cloud(response, breaker)
