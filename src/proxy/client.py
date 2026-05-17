"""Async OpenAI-compatible provider client."""

import json
import logging
from typing import AsyncIterator, Any

import httpx

from .config import Settings

logger = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=5.0)


class ProviderError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(message)


class ProviderClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.provider_base_url,
            headers={"Authorization": f"Bearer {settings.provider_api_key}"},
            timeout=TIMEOUT,
        )

    async def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = {**payload, "stream": False}
        resp = await self._client.post("/chat/completions", json=payload)
        if resp.status_code != 200:
            raise ProviderError(resp.status_code, resp.text)
        return resp.json()

    async def stream(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        payload = {**payload, "stream": True, "stream_options": {"include_usage": True}}
        async with self._client.stream("POST", "/chat/completions", json=payload) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise ProviderError(resp.status_code, body.decode())
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    logger.warning("Unparseable SSE chunk: %s", data)

    async def aclose(self):
        await self._client.aclose()
