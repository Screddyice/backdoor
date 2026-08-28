"""Bounded, fail-open recall from the authoritative local Cognee server."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import Settings

logger = logging.getLogger(__name__)


def _text_from_result(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, dict):
        for key in ("text", "content", "context"):
            if isinstance(value.get(key), str):
                return " ".join(value[key].split())
    return ""


async def recall_context(
    query: str,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[str]:
    """Return relevant graph context, or an empty list on any recall failure."""
    headers = {"content-type": "application/json"}
    if settings.cognee_api_key:
        headers["x-api-key"] = settings.cognee_api_key
    payload = {
        "query": query,
        "top_k": settings.codex_cognee_top_k,
        "only_context": True,
        "scope": ["graph"],
    }

    try:
        async with httpx.AsyncClient(
            base_url=settings.cognee_base_url.rstrip("/"),
            timeout=settings.codex_cognee_timeout_seconds,
            transport=transport,
        ) as client:
            response = await client.post("/api/v1/recall", json=payload, headers=headers)
        response.raise_for_status()
        values = response.json()
        if not isinstance(values, list):
            logger.warning("Cognee recall returned an unexpected result type")
            return []
    except httpx.HTTPStatusError as exc:
        logger.warning("Cognee recall failed with HTTP %d", exc.response.status_code)
        return []
    except httpx.HTTPError as exc:
        logger.warning("Cognee recall failed (%s)", type(exc).__name__)
        return []
    except ValueError:
        logger.warning("Cognee recall returned invalid JSON")
        return []

    recalled: list[str] = []
    seen: set[str] = set()
    used = 0
    for value in values:
        text = _text_from_result(value)
        if not text or text in seen:
            continue
        if used + len(text) > settings.codex_cognee_char_budget:
            continue
        recalled.append(text)
        seen.add(text)
        used += len(text)
    return recalled
