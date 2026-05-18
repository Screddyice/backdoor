"""FastAPI route handlers."""

import logging
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from .config import Settings, get_settings
from .models import MessagesRequest, TokenCountRequest, MessagesResponse, TokenCountResponse, Usage
from .client import ProviderClient, ProviderError
from .tokens import count_messages
from .translate import build_nim_payload, nim_response_to_anthropic, stream_openai_to_anthropic
from .optimizations import (
    is_quota_probe, is_title_generation, is_suggestion_mode,
    is_prefix_detection, extract_prefix,
    is_filepath_extraction, extract_filepaths,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_client: ProviderClient | None = None


def set_provider_client(c: ProviderClient):
    global _client
    _client = c


def get_provider_client() -> ProviderClient:
    if _client is None:
        raise RuntimeError("Provider client not initialised")
    return _client


def _mock_response(req: MessagesRequest, text: str) -> MessagesResponse:
    return MessagesResponse(
        id=f"msg_{uuid.uuid4().hex}",
        model=req.model,
        content=[{"type": "text", "text": text}],
        stop_reason="end_turn",
        usage=Usage(input_tokens=10, output_tokens=len(text.split())),
    )


def _check_optimizations(req: MessagesRequest, settings: Settings) -> MessagesResponse | None:
    if settings.skip_quota_probes and is_quota_probe(req):
        logger.debug("intercepted quota probe")
        return _mock_response(req, "ok")

    if settings.skip_title_generation and is_title_generation(req):
        logger.debug("intercepted title generation")
        return _mock_response(req, "Conversation")

    if settings.skip_suggestion_mode and is_suggestion_mode(req):
        logger.debug("intercepted suggestion mode")
        return _mock_response(req, "")

    if settings.mock_prefix_detection:
        hit, cmd = is_prefix_detection(req)
        if hit:
            logger.debug("intercepted prefix detection")
            return _mock_response(req, extract_prefix(cmd))

    if settings.mock_filepath_extraction:
        hit, cmd, output = is_filepath_extraction(req)
        if hit:
            logger.debug("intercepted filepath extraction")
            return _mock_response(req, extract_filepaths(output))

    return None


@router.post("/v1/messages")
async def create_message(
    req: MessagesRequest,
    settings: Settings = Depends(get_settings),
):
    fast = _check_optimizations(req, settings)
    if fast:
        return fast

    client = get_provider_client()
    payload = build_nim_payload(req, settings)
    msg_id = f"msg_{uuid.uuid4().hex}"
    last_user = next((m.content for m in reversed(req.messages) if m.role == "user"), "")
    preview = (last_user if isinstance(last_user, str) else str(last_user))[:80]
    mode = "stream" if req.stream else "complete"
    provider = settings.provider_model
    logger.info("→ %s [%s] tools=%s | %r", provider, mode, len(req.tools or []), preview)

    if req.stream:
        input_tokens = count_messages(req.messages, req.system, req.tools)
        return StreamingResponse(
            _stream(client, payload, msg_id, req, input_tokens, provider),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        resp = await client.complete(payload)
    except ProviderError as e:
        logger.error("Provider error %s: %s", e.status_code, e.message)
        raise HTTPException(status_code=e.status_code, detail=e.message)

    result = nim_response_to_anthropic(resp, req, msg_id)
    usage = result.get("usage", {})
    logger.info(
        "← %s [%s] stop=%s out_tokens=%s in_tokens=%s",
        provider, mode,
        result.get("stop_reason"),
        usage.get("output_tokens"),
        usage.get("input_tokens"),
    )
    return result


async def _stream(
    client: ProviderClient,
    payload: dict,
    msg_id: str,
    req: MessagesRequest,
    input_tokens: int,
    provider: str,
) -> AsyncIterator[str]:
    state: dict = {}
    try:
        async for chunk in client.stream(payload):
            for event in stream_openai_to_anthropic(chunk, state, msg_id, req, input_tokens):
                yield event
        logger.info("← %s [stream] done in_tokens=%s", provider, input_tokens)
    except ProviderError as e:
        logger.error("Provider stream error %s: %s", e.status_code, e.message)
        import json
        yield f"event: error\ndata: {json.dumps({'type':'error','error':{'type':'api_error','message':e.message}})}\n\n"


@router.post("/v1/messages/count_tokens")
async def count_tokens(req: TokenCountRequest):
    return TokenCountResponse(input_tokens=count_messages(req.messages, req.system, req.tools))


@router.get("/health")
async def health():
    return {"status": "ok"}
