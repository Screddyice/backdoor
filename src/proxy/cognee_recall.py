"""Bounded, fail-open recall from the authoritative local Cognee server."""

from __future__ import annotations

import logging
import json
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

from .config import Settings

logger = logging.getLogger(__name__)

_COGNEE_ENV = Path.home() / ".cognee" / ".env"
_COGNEE_KEY_CACHE = Path.home() / ".cognee-plugin" / "api_key.json"


def _text_from_result(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, dict):
        for key in ("text", "content", "context"):
            if isinstance(value.get(key), str):
                return " ".join(value[key].split())
    return ""


def resolve_cognee_api_key(
    settings: Settings,
    env_path: Path | None = None,
    cache_path: Path | None = None,
) -> str:
    """Resolve Cognee auth from its existing stores without copying the key."""
    if settings.cognee_api_key.strip():
        return settings.cognee_api_key.strip()

    source = _COGNEE_ENV if env_path is None else env_path
    try:
        from_file = str(dotenv_values(source).get("COGNEE_API_KEY") or "").strip()
        if from_file:
            return from_file
    except (OSError, ValueError):
        pass

    cached_source = _COGNEE_KEY_CACHE if cache_path is None else cache_path
    try:
        cached = json.loads(cached_source.read_text(encoding="utf-8"))
        if not isinstance(cached, dict):
            return ""
        key = str(cached.get("api_key") or "").strip()
        cached_url = str(cached.get("base_url") or "").strip().rstrip("/")
        wanted_url = settings.cognee_base_url.strip().rstrip("/")
        if key and (not cached_url or cached_url == wanted_url):
            return key
    except (OSError, ValueError, TypeError):
        pass
    return ""


async def recall_context(
    query: str,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[str]:
    """Return relevant graph context, or an empty list on any recall failure."""
    headers = {"content-type": "application/json"}
    api_key = resolve_cognee_api_key(settings)
    if api_key:
        headers["x-api-key"] = api_key
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
