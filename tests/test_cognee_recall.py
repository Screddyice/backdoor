import json
import logging

import httpx
import pytest

from src.proxy.config import Settings
from src.proxy.cognee_recall import recall_context


@pytest.mark.asyncio
async def test_recall_posts_authoritative_graph_query_and_normalizes_results():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=[
                {"text": " decision   one "},
                "decision two",
                {"content": "decision one"},
                {"context": "decision three"},
            ],
        )

    settings = Settings(
        cognee_base_url="http://cognee.test",
        cognee_api_key="test-key",
        codex_cognee_top_k=8,
        codex_cognee_char_budget=100,
    )
    result = await recall_context(
        "active user task",
        settings,
        transport=httpx.MockTransport(handler),
    )

    assert result == ["decision one", "decision two", "decision three"]
    assert len(seen) == 1
    assert seen[0].url == httpx.URL("http://cognee.test/api/v1/recall")
    assert seen[0].headers["x-api-key"] == "test-key"
    assert json.loads(seen[0].content) == {
        "query": "active user task",
        "top_k": 8,
        "only_context": True,
        "scope": ["graph"],
    }


@pytest.mark.asyncio
async def test_recall_omits_empty_api_key_and_treats_empty_list_as_success(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "x-api-key" not in request.headers
        return httpx.Response(200, json=[])

    settings = Settings(cognee_base_url="http://cognee.test", cognee_api_key="")
    with caplog.at_level(logging.WARNING):
        result = await recall_context(
            "no matching context",
            settings,
            transport=httpx.MockTransport(handler),
        )

    assert result == []
    assert caplog.records == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["connect", "timeout", "unauthorized", "invalid-json"])
async def test_recall_failures_return_no_context_without_logging_sensitive_data(
    failure, caplog
):
    secret_query = "private-query-marker"
    secret_key = "private-key-marker"

    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "connect":
            raise httpx.ConnectError("connect failed", request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout("read timed out", request=request)
        if failure == "unauthorized":
            return httpx.Response(401, json={"detail": "private-response-marker"})
        return httpx.Response(200, content=b"not-json")

    settings = Settings(
        cognee_base_url="http://cognee.test",
        cognee_api_key=secret_key,
    )
    with caplog.at_level(logging.WARNING):
        result = await recall_context(
            secret_query,
            settings,
            transport=httpx.MockTransport(handler),
        )

    rendered = caplog.text
    assert result == []
    assert secret_query not in rendered
    assert secret_key not in rendered
    assert "private-response-marker" not in rendered


@pytest.mark.asyncio
async def test_recall_stops_before_exceeding_character_budget():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["12345", "67890", "x"])

    settings = Settings(
        cognee_base_url="http://cognee.test",
        codex_cognee_char_budget=9,
    )
    result = await recall_context(
        "bounded recall",
        settings,
        transport=httpx.MockTransport(handler),
    )

    assert result == ["12345", "x"]
