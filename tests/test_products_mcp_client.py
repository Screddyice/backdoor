"""Contract tests for the account-synced product gateway."""

from __future__ import annotations

import json

import httpx
import pytest

from src.products_mcp.client import ProductGateway, ProductSettings, UpstreamError


OPENAPI = {
    "openapi": "3.1.0",
    "paths": {
        "/health": {
            "get": {
                "operationId": "health_health_get",
                "summary": "Read service health",
            }
        },
        "/accounts/{account_id}": {
            "post": {
                "operationId": "accounts_update",
                "summary": "Update one account",
                "parameters": [
                    {
                        "name": "account_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "dry_run",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "boolean"},
                    },
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"type": "object", "properties": {"name": {"type": "string"}}}
                        }
                    },
                },
            }
        },
    },
}


def settings() -> ProductSettings:
    return ProductSettings(
        hypercrawl_url="https://crawl.example/v1",
        hypercrawl_token="hc_test_value",
        hyperscale_url="https://scale.example/mcp",
        hyperscale_key="hf_test_value",
        engagemate_url="http://127.0.0.1:13100",
        engagemate_key="engage_test_value",
        engagemate_user_id="user_test",
    )


@pytest.mark.parametrize(
    ("product", "environment"),
    [
        (
            "hypercrawl",
            {
                "HYPERCRAWL_URL": "https://crawl.example/v1",
                "HYPERCRAWL_REST_TOKEN": "hc_test_value",
            },
        ),
        ("hyperscale", {"HYPERFLOW_API_KEY": "hf_test_value"}),
        (
            "engagemate",
            {
                "ENGAGEMATE_URL": "http://127.0.0.1:13100",
                "INTERNAL_API_KEY": "engage_test_value",
                "ENGAGEMATE_USER_ID": "user_test",
            },
        ),
    ],
)
def test_selected_product_settings_do_not_require_other_product_credentials(
    monkeypatch, product, environment
) -> None:
    for name in (
        "HYPERCRAWL_URL",
        "HYPERCRAWL_REST_TOKEN",
        "HYPERFLOW_API_KEY",
        "ENGAGEMATE_URL",
        "ENGAGEMATE_INTERNAL_API_KEY",
        "INTERNAL_API_KEY",
        "ENGAGEMATE_USER_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    loaded = ProductSettings.from_env(product)

    if product == "hypercrawl":
        assert loaded.hypercrawl_token == "hc_test_value"
    elif product == "hyperscale":
        assert loaded.hyperscale_key == "hf_test_value"
    else:
        assert loaded.engagemate_key == "engage_test_value"


def test_selected_product_settings_reject_missing_required_credential(
    monkeypatch,
) -> None:
    monkeypatch.setenv("HYPERCRAWL_URL", "https://crawl.example/v1")
    monkeypatch.delenv("HYPERCRAWL_REST_TOKEN", raising=False)

    with pytest.raises(ValueError, match="HYPERCRAWL_REST_TOKEN"):
        ProductSettings.from_env("hypercrawl")


@pytest.mark.asyncio
async def test_openapi_catalog_and_call_preserve_declared_route_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/v1/openapi.json":
            return httpx.Response(200, json=OPENAPI)
        if request.url.path == "/v1/accounts/acct_1":
            return httpx.Response(200, json={"updated": True})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = ProductGateway(settings(), http=client)
        tools = await gateway.list_tools("hypercrawl")
        result = await gateway.call_tool(
            "hypercrawl",
            "accounts_update",
            {"account_id": "acct_1", "dry_run": True, "body": {"name": "New"}},
        )

    assert [tool["name"] for tool in tools] == ["health_health_get", "accounts_update"]
    assert tools[1]["inputSchema"]["required"] == ["account_id", "body"]
    assert result == {"status": 200, "body": {"updated": True}}
    assert requests[-1].url.query == b"dry_run=true"
    assert json.loads(requests[-1].content) == {"name": "New"}
    assert requests[-1].headers["authorization"] == "Bearer hc_test_value"


@pytest.mark.asyncio
async def test_openapi_routes_without_operation_ids_get_stable_names_and_no_double_prefix() -> None:
    document = {
        "openapi": "3.1.0",
        "paths": {
            "/v1/crawl": {
                "post": {
                    "summary": "Crawl one URL",
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    },
                }
            }
        },
    }
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/openapi.json":
            return httpx.Response(200, json=document)
        if request.url.path == "/v1/crawl":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = ProductGateway(settings(), http=client)
        tools = await gateway.list_tools("hypercrawl")
        result = await gateway.call_tool("hypercrawl", "post_v1_crawl", {"body": {}})

    assert [tool["name"] for tool in tools] == ["post_v1_crawl"]
    assert result["body"] == {"ok": True}
    assert requests[-1].url.path == "/v1/crawl"


@pytest.mark.asyncio
async def test_hyperscale_forwards_native_mcp_list_and_call() -> None:
    messages: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        messages.append(json.loads(request.content))
        method = messages[-1]["method"]
        result = (
            {"tools": [{"name": "campaign_list_templates", "inputSchema": {"type": "object"}}]}
            if method == "tools/list"
            else {"content": [{"type": "text", "text": "ok"}]}
        )
        return httpx.Response(
            200,
            text=f"event: message\ndata: {json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': result})}\n\n",
            headers={"content-type": "text/event-stream"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = ProductGateway(settings(), http=client)
        tools = await gateway.list_tools("hyperscale")
        result = await gateway.call_tool("hyperscale", "campaign_list_templates", {})

    assert tools[0]["name"] == "campaign_list_templates"
    assert result == {"content": [{"type": "text", "text": "ok"}]}
    assert [message["method"] for message in messages] == ["tools/list", "tools/call"]
    assert messages[-1]["params"] == {"name": "campaign_list_templates", "arguments": {}}


@pytest.mark.asyncio
async def test_engagemate_uses_internal_key_and_user_headers() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=OPENAPI)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = ProductGateway(settings(), http=client)
        await gateway.list_tools("engagemate")
        result = await gateway.call_tool("engagemate", "health_health_get", {})

    assert result == {"status": 200, "body": {"ok": True}}
    assert requests[-1].headers["x-internal-api-key"] == "engage_test_value"
    assert requests[-1].headers["x-user-id"] == "user_test"


@pytest.mark.asyncio
async def test_unknown_tool_cannot_become_an_arbitrary_upstream_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=OPENAPI)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = ProductGateway(settings(), http=client)
        with pytest.raises(UpstreamError, match="not advertised"):
            await gateway.call_tool("hypercrawl", "../../admin", {})


@pytest.mark.asyncio
async def test_openapi_call_rejects_missing_body_and_unknown_arguments() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=OPENAPI)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = ProductGateway(settings(), http=client)
        with pytest.raises(UpstreamError, match="requires request body"):
            await gateway.call_tool("engagemate", "accounts_update", {"account_id": "a"})
        with pytest.raises(UpstreamError, match="unexpected argument"):
            await gateway.call_tool(
                "engagemate",
                "accounts_update",
                {"account_id": "a", "body": {}, "misspelled": True},
            )


@pytest.mark.asyncio
async def test_upstream_errors_hide_response_bodies_and_credentials() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="database password=hunter2")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = ProductGateway(settings(), http=client)
        with pytest.raises(UpstreamError) as caught:
            await gateway.list_tools("hyperscale")

    message = str(caught.value)
    assert "500" in message
    assert "hunter2" not in message
    assert "hf_test_value" not in message
