"""Tool-surface tests for the account-synced products MCP server."""

from __future__ import annotations

import pytest

import src.products_mcp.server as products_server
from src.hermes_mcp.http_server import TransportSecuritySettings
from src.products_mcp.server import build_server


EXPECTED_TOOLS = {
    "hypercrawl": {
        "hypercrawl_list_tools",
        "hypercrawl_call",
        "hypercrawl_status",
    },
    "hyperscale": {
        "hyperscale_list_tools",
        "hyperscale_call",
        "hyperscale_status",
    },
    "engagemate": {
        "engagemate_list_tools",
        "engagemate_call",
        "engagemate_status",
    },
}


EXPECTED_ROUTING = {
    "hypercrawl": {
        "title": "HyperCrawl",
        "purpose": "web research",
        "exclude": "LinkedIn outreach",
        "owner": "Team Nebula",
    },
    "hyperscale": {
        "title": "HyperScale",
        "purpose": "LinkedIn prospects",
        "exclude": "web crawling",
        "owner": "Team Nebula",
    },
    "engagemate": {
        "title": "EngageMate",
        "purpose": "Instagram",
        "exclude": "LinkedIn outreach",
        "owner": "Shawn Reddy Consulting",
    },
}


class FakeGateway:
    async def list_tools(self, product: str):
        return [{"name": f"{product}_read", "inputSchema": {"type": "object"}}]

    async def call_tool(self, product: str, tool_name: str, arguments: dict):
        return {"product": product, "tool": tool_name, "arguments": arguments}


@pytest.mark.asyncio
@pytest.mark.parametrize("product", ["hypercrawl", "hyperscale", "engagemate"])
async def test_server_registers_only_selected_product_tools(
    monkeypatch, product
) -> None:
    monkeypatch.setenv("HERMES_MCP_OAUTH_ISSUER", "https://products.example")
    monkeypatch.setenv("HERMES_MCP_OAUTH_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.setenv("HERMES_MCP_OAUTH_REDIRECT_HOSTS", "claude.ai,claude.com")
    monkeypatch.setenv("HERMES_MCP_OAUTH_STATE_PATH", "/tmp/products-mcp-test-state.json")

    server = build_server(product=product, gateway=FakeGateway())
    tools = await server.list_tools()

    assert {tool.name for tool in tools} == EXPECTED_TOOLS[product]


@pytest.mark.parametrize("product", ["hypercrawl", "hyperscale", "engagemate"])
def test_server_advertises_product_specific_routing_instructions(
    monkeypatch, tmp_path, product
) -> None:
    monkeypatch.setenv("HERMES_MCP_OAUTH_ISSUER", "https://products.example")
    monkeypatch.setenv("HERMES_MCP_OAUTH_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.setenv("HERMES_MCP_OAUTH_REDIRECT_HOSTS", "claude.ai,claude.com")
    monkeypatch.setenv("HERMES_MCP_OAUTH_STATE_PATH", str(tmp_path / "state.json"))

    server = build_server(product=product, gateway=FakeGateway())
    expected = EXPECTED_ROUTING[product]

    assert server.title == expected["title"]
    assert expected["purpose"] in server.description
    assert expected["exclude"] in server.instructions
    assert expected["owner"] in server.instructions
    assert f"{product}_status" in server.instructions
    assert f"{product}_list_tools" in server.instructions
    assert "explicit request" in server.instructions


def test_server_requires_product_selection(monkeypatch) -> None:
    monkeypatch.delenv("PRODUCTS_MCP_PRODUCT", raising=False)

    with pytest.raises(ValueError, match="PRODUCTS_MCP_PRODUCT must select"):
        build_server(gateway=FakeGateway())


def test_server_rejects_unknown_product(monkeypatch) -> None:
    monkeypatch.setenv("PRODUCTS_MCP_PRODUCT", "all-products")

    with pytest.raises(ValueError, match="unknown product"):
        build_server(gateway=FakeGateway())


@pytest.mark.asyncio
async def test_status_and_call_tools_return_product_scoped_results(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HERMES_MCP_OAUTH_ISSUER", "https://products.example")
    monkeypatch.setenv("HERMES_MCP_OAUTH_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.setenv("HERMES_MCP_OAUTH_REDIRECT_HOSTS", "claude.ai,claude.com")
    monkeypatch.setenv("HERMES_MCP_OAUTH_STATE_PATH", str(tmp_path / "state.json"))

    server = build_server(product="hypercrawl", gateway=FakeGateway())
    status = await server.call_tool("hypercrawl_status", {})
    called = await server.call_tool(
        "hypercrawl_call",
        {"tool_name": "account_list", "arguments": {"limit": 2}},
    )

    assert status.structured_content == {
        "product": "hypercrawl",
        "ok": True,
        "toolCount": 1,
    }
    assert called.structured_content == {
        "product": "hypercrawl",
        "tool": "account_list",
        "arguments": {"limit": 2},
    }


def test_main_passes_configured_bind_to_explicit_http_runner(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("HERMES_MCP_PORT", "8010")
    monkeypatch.setenv("PRODUCTS_MCP_PRODUCT", "hypercrawl")
    monkeypatch.setenv(
        "HERMES_MCP_ALLOWED_HOSTS",
        "screddy-products.5-161-126-205.sslip.io:443",
    )
    captured = {}

    class FakeServer:
        def run(self, **kwargs):
            raise AssertionError("main() used the generic runner")

        async def run_streamable_http_async(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(products_server, "build_server", lambda: FakeServer())

    products_server.main()

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8010
    assert isinstance(captured["transport_security"], TransportSecuritySettings)
