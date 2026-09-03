import json
import logging
import httpx
import pytest

from src.proxy.config import Settings
from src.proxy.cognee_recall import recall_context, resolve_cognee_api_key


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
async def test_recall_omits_empty_api_key_and_treats_empty_list_as_success(
    caplog, monkeypatch
):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "x-api-key" not in request.headers
        return httpx.Response(200, json=[])

    settings = Settings(cognee_base_url="http://cognee.test", cognee_api_key="")
    monkeypatch.setattr("src.proxy.cognee_recall.resolve_cognee_api_key", lambda _: "")
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


def test_api_key_resolution_uses_existing_cognee_files_without_copying(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text('COGNEE_API_KEY="env-file-key"\n', encoding="utf-8")
    cache_path = tmp_path / "api_key.json"
    cache_path.write_text(
        json.dumps({"api_key": "cached-key", "base_url": "http://127.0.0.1:8001"}),
        encoding="utf-8",
    )

    assert resolve_cognee_api_key(
        Settings(cognee_api_key="explicit-key"),
        env_path=env_path,
        cache_path=cache_path,
    ) == "explicit-key"
    assert resolve_cognee_api_key(
        Settings(cognee_api_key=""),
        env_path=env_path,
        cache_path=cache_path,
    ) == "env-file-key"

    env_path.write_text("", encoding="utf-8")
    assert resolve_cognee_api_key(
        Settings(cognee_api_key=""),
        env_path=env_path,
        cache_path=cache_path,
    ) == "cached-key"


def test_cached_cognee_key_is_ignored_for_a_different_server(tmp_path):
    cache_path = tmp_path / "api_key.json"
    cache_path.write_text(
        json.dumps({"api_key": "wrong-server-key", "base_url": "http://other.test"}),
        encoding="utf-8",
    )

    assert resolve_cognee_api_key(
        Settings(cognee_base_url="http://127.0.0.1:8001", cognee_api_key=""),
        env_path=tmp_path / "missing.env",
        cache_path=cache_path,
    ) == ""
