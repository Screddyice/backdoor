"""Provider-edge 404s must not masquerade as healthy API responses."""

import json

import httpx
import pytest
from fastapi import Request
from fastapi.responses import Response

import src.proxy.codex_routes as codex_routes
import src.proxy.routes as routes
from src.proxy.app import create_app
from src.proxy.config import Settings, get_settings
from src.proxy.failover import FailoverBreaker
from src.proxy.provider_errors import is_provider_edge_404


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes):
        self.content = content

    async def __aiter__(self):
        yield self.content


def _request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("backdoor.test", 80),
        }
    )


def _breaker(tmp_path, source: str, *, require_offline: bool) -> FailoverBreaker:
    return FailoverBreaker(
        threshold=1,
        min_outage=0,
        notify_fn=lambda *_: None,
        online_fn=lambda: True,
        state_path=tmp_path / f"{source}.json",
        source=source,
        upstream_name=source,
        require_offline=require_offline,
    )


@pytest.mark.parametrize(
    ("status", "headers", "body", "expected"),
    [
        (404, {"cf-ray": "ray", "content-type": "text/html"}, b"", True),
        (
            404,
            {"cf-ray": "ray", "content-type": "text/html"},
            b"<html>missing</html>",
            True,
        ),
        (
            404,
            {"cf-ray": "ray", "content-type": "application/json"},
            b"not-json",
            True,
        ),
        (
            404,
            {"cf-ray": "ray", "content-type": "application/problem+json"},
            b'{"error":{"message":"model missing"}}',
            False,
        ),
        (
            404,
            {"cf-ray": "ray", "content-type": "application/json"},
            b'{"detail":"not found"}',
            False,
        ),
        (404, {"content-type": "text/html"}, b"", False),
        (503, {"cf-ray": "ray", "content-type": "text/html"}, b"", False),
    ],
)
def test_provider_edge_404_classifier(status, headers, body, expected):
    assert is_provider_edge_404(status, headers, body) is expected


@pytest.mark.asyncio
async def test_claude_edge_404_activates_failover(monkeypatch, tmp_path):
    # Production Claude policy is offline-gated. A positively identified
    # provider-edge outage must still open while the rest of the internet works.
    breaker = _breaker(tmp_path, "anthropic", require_offline=True)
    monkeypatch.setattr(routes, "_breaker", breaker)

    async def edge_404(*_args):
        return httpx.Response(
            404,
            stream=BytesStream(b""),
            headers={"cf-ray": "edge-ray-DPS", "content-type": "text/html"},
        )

    monkeypatch.setattr(routes, "_upstream_send", edge_404)

    relay = await routes._try_upstream(
        _request("/v1/messages"), b"{}", Settings(router_mode="hybrid")
    )

    assert relay is None
    assert breaker.open is True
    assert breaker.reason == "HTTP 404 provider edge"


@pytest.mark.asyncio
async def test_claude_structured_api_404_stays_visible(monkeypatch, tmp_path):
    breaker = _breaker(tmp_path, "anthropic", require_offline=True)
    monkeypatch.setattr(routes, "_breaker", breaker)
    body = b'{"type":"error","error":{"type":"not_found_error","message":"model missing"}}'

    async def api_404(*_args):
        return httpx.Response(
            404,
            stream=BytesStream(body),
            headers={"cf-ray": "api-ray-DPS", "content-type": "application/json"},
        )

    monkeypatch.setattr(routes, "_upstream_send", api_404)

    relay = await routes._try_upstream(
        _request("/v1/messages"), b"{}", Settings(router_mode="hybrid")
    )

    assert relay.status_code == 404
    assert relay.body == body
    assert breaker.open is False
    assert breaker._failures == 0


@pytest.mark.asyncio
async def test_codex_edge_404_activates_failover(monkeypatch, tmp_path):
    settings = Settings(
        codex_chatgpt_upstream="https://chatgpt.test/backend-api/codex",
    )
    breaker = _breaker(tmp_path, "codex", require_offline=False)
    monkeypatch.setattr(codex_routes, "_codex_breaker", breaker)

    def edge_404(_request):
        return httpx.Response(
            404,
            stream=BytesStream(b""),
            headers={"cf-ray": "edge-ray-DPS", "content-type": "text/html"},
        )

    async def serve_local(*_args):
        return Response("local", media_type="text/plain")

    monkeypatch.setattr(
        codex_routes,
        "_chatgpt_client",
        httpx.AsyncClient(
            base_url=settings.codex_chatgpt_upstream,
            transport=httpx.MockTransport(edge_404),
        ),
    )
    monkeypatch.setattr(codex_routes, "_serve_local", serve_local)

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    payload = json.dumps({"model": "gpt-5.6-sol", "input": []}).encode()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://backdoor.test"
    ) as client:
        response = await client.post("/backend-api/codex/responses", content=payload)

    assert response.status_code == 200
    assert response.text == "local"
    assert breaker.open is True
    assert breaker.reason == "HTTP 404 provider edge"


@pytest.mark.asyncio
async def test_codex_structured_api_404_stays_visible(monkeypatch, tmp_path):
    settings = Settings(
        codex_chatgpt_upstream="https://chatgpt.test/backend-api/codex",
    )
    breaker = _breaker(tmp_path, "codex", require_offline=False)
    monkeypatch.setattr(codex_routes, "_codex_breaker", breaker)
    body = b'{"error":{"type":"not_found_error","message":"model missing"}}'
    local_calls = 0

    def api_404(_request):
        return httpx.Response(
            404,
            stream=BytesStream(body),
            headers={"cf-ray": "api-ray-DPS", "content-type": "application/json"},
        )

    async def serve_local(*_args):
        nonlocal local_calls
        local_calls += 1
        return Response("local")

    monkeypatch.setattr(
        codex_routes,
        "_chatgpt_client",
        httpx.AsyncClient(
            base_url=settings.codex_chatgpt_upstream,
            transport=httpx.MockTransport(api_404),
        ),
    )
    monkeypatch.setattr(codex_routes, "_serve_local", serve_local)

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    payload = json.dumps({"model": "gpt-5.6-sol", "input": []}).encode()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://backdoor.test"
    ) as client:
        response = await client.post("/backend-api/codex/responses", content=payload)

    assert response.status_code == 404
    assert response.content == body
    assert local_calls == 0
    assert breaker.open is False
    assert breaker._failures == 0
