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
    )


@pytest.mark.parametrize(
    ("product", "environment"),
    [
        (
            "hypercrawl",
            {
                "HYPERCRAWL_MCP_URL": "https://crawl.example/v1",
                "HYPERCRAWL_MCP_TOKEN": "hc_test_value",
            },
        ),
        ("hyperscale", {"HYPERFLOW_API_KEY": "hf_test_value"}),
    ],
)
def test_selected_product_settings_do_not_require_other_product_credentials(
    monkeypatch, product, environment
) -> None:
    for name in (
        "HYPERCRAWL_URL",
        "HYPERCRAWL_REST_TOKEN",
        "HYPERCRAWL_MCP_URL",
        "HYPERCRAWL_MCP_TOKEN",
        "HYPERFLOW_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    loaded = ProductSettings.from_env(product)

    if product == "hypercrawl":
        assert loaded.hypercrawl_token == "hc_test_value"
    else:
        assert loaded.hyperscale_key == "hf_test_value"


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


def test_engagemate_is_no_longer_a_gateway_product() -> None:
    fields = set(ProductSettings.__dataclass_fields__)

    assert not {name for name in fields if name.startswith("engagemate")}


@pytest.mark.asyncio
async def test_engagemate_requests_are_refused_without_reaching_upstream() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=OPENAPI)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = ProductGateway(settings(), http=client)
        with pytest.raises(UpstreamError, match="unknown product: engagemate"):
            await gateway.list_tools("engagemate")  # type: ignore[arg-type]

    assert requests == []


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
            await gateway.call_tool("hypercrawl", "accounts_update", {"account_id": "a"})
        with pytest.raises(UpstreamError, match="unexpected argument"):
            await gateway.call_tool(
                "hypercrawl",
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


@pytest.mark.asyncio
@pytest.mark.parametrize("product,status,payload,typed", [
    ("hypercrawl", 503, {"code": "SEARCH_UNAVAILABLE", "blocked": True}, True),
    ("hypercrawl", 503, {"code": "SEARCH_UNAVAILABLE", "blocked": 1}, False),
    ("hypercrawl", 503, {"code": "SEARCH_UNAVAILABLE", "blocked": False}, False),
    ("hypercrawl", 503, {"code": "OTHER_ERROR", "blocked": True}, False),
    ("hypercrawl", 500, {"code": "SEARCH_UNAVAILABLE", "blocked": True}, False),
    ("hypercrawl", 503, ["SEARCH_UNAVAILABLE"], False),
    ("hypercrawl", 503, "not-json", False),
])
async def test_search_unavailable_preserves_only_safe_typed_details(product, status, payload, typed):
    if isinstance(payload, dict):
        payload = {**payload, "error": "private upstream detail", "diagnostics": "private upstream detail"}

    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(payload, str):
            return httpx.Response(status, text=payload)
        return httpx.Response(status, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = ProductGateway(settings(), http=client)
        with pytest.raises(UpstreamError) as caught:
            await gateway._request_json(product, "POST", "https://public.example/v1/search", headers={})

    message = str(caught.value)
    assert ("SEARCH_UNAVAILABLE" in message) is typed
    assert ("blocked=true" in message) is typed
    assert "private upstream detail" not in message
    assert "not-json" not in message
