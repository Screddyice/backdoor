"""Async OpenAI-compatible provider client."""

import asyncio
import json
import logging
from typing import AsyncIterator, Any
from urllib.parse import urlparse

import httpx

from .config import Settings

logger = logging.getLogger(__name__)

# read=600: a local model cold-prefilling a 50K-token harness prompt emits no
# bytes for several minutes; 120s here silently killed those streams mid-prefill
# (the client then retried, re-prefilling from scratch — doubling every turn).
TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=5.0)
LOCAL_TIMEOUT = httpx.Timeout(connect=120.0, read=600.0, write=60.0, pool=600.0)
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class ProviderError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(message)


class ProviderClient:
    def __init__(self, settings: Settings):
        self._settings = settings
        is_local = urlparse(settings.provider_base_url).hostname in _LOCAL_HOSTS
        self._gate = asyncio.Semaphore(1) if is_local else None
        self._client = httpx.AsyncClient(
            base_url=settings.provider_base_url,
            headers={"Authorization": f"Bearer {settings.provider_api_key}"},
            timeout=LOCAL_TIMEOUT if is_local else TIMEOUT,
        )

    async def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._gate is not None:
            async with self._gate:
                return await self._complete(payload)
        return await self._complete(payload)

    async def _complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = {**payload, "stream": False}
        resp = await self._client.post("/chat/completions", json=payload)
        if resp.status_code != 200:
            raise ProviderError(resp.status_code, resp.text)
        return resp.json()

    async def stream(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        if self._gate is not None:
            async with self._gate:
                async for chunk in self._stream(payload):
                    yield chunk
            return
        async for chunk in self._stream(payload):
            yield chunk

    async def _stream(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
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
