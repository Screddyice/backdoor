"""Regression tests for transient cloud failures and local failover pressure."""

import asyncio

import httpx
import pytest
from starlette.requests import Request

import src.proxy.routes as routes
from src.proxy.client import ProviderClient
from src.proxy.config import Settings


def _request(path: str = "/v1/messages") -> Request:
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8083),
    })


@pytest.mark.asyncio
async def test_transient_connect_failure_retries_once_before_reaching_the_client():
    attempts = []

    def fail_then_succeed(request: httpx.Request) -> httpx.Response:
        if not attempts:
            attempts.append("failed")
            raise httpx.ConnectTimeout("timed out", request=request)
        attempts.append("succeeded")
        return httpx.Response(200, content=b"ok", request=request)

    upstream = httpx.AsyncClient(
        base_url="https://api.anthropic.com",
        transport=httpx.MockTransport(fail_then_succeed),
    )
    routes._upstream_client = upstream

    try:
        response = await routes._upstream_send(_request(), b"{}", Settings())
        assert response.status_code == 200
        assert attempts == ["failed", "succeeded"]
        await response.aclose()
    finally:
        routes._upstream_client = None
        await upstream.aclose()


class _BlockedLocalHTTPClient:
    def __init__(self):
        self.calls = 0
        self.first_entered = asyncio.Event()
        self.release = asyncio.Event()

    async def post(self, path, json):
        self.calls += 1
        self.first_entered.set()
        await self.release.wait()
        return httpx.Response(200, json={"ok": True})

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_local_provider_serializes_requests_instead_of_flooding_ollama():
    provider = ProviderClient(Settings(provider_base_url="http://127.0.0.1:11434/v1"))
    await provider._client.aclose()
    blocked = _BlockedLocalHTTPClient()
    provider._client = blocked

    first = asyncio.create_task(provider.complete({"messages": []}))
    await blocked.first_entered.wait()
    second = asyncio.create_task(provider.complete({"messages": []}))
    await asyncio.sleep(0)

    assert blocked.calls == 1, "a retry opened another Ollama request while one was running"

    blocked.release.set()
    assert await first == {"ok": True}
    assert await second == {"ok": True}
