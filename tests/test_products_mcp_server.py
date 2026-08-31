"""Tool-surface tests for the account-synced products MCP server."""

from __future__ import annotations

import pytest

from src.products_mcp.server import build_server


EXPECTED_TOOLS = {
    "hypercrawl_list_tools",
    "hypercrawl_call",
    "hypercrawl_status",
    "hyperscale_list_tools",
    "hyperscale_call",
    "hyperscale_status",
    "engagemate_list_tools",
    "engagemate_call",
    "engagemate_status",
}


class FakeGateway:
    async def list_tools(self, product: str):
        return [{"name": f"{product}_read", "inputSchema": {"type": "object"}}]

    async def call_tool(self, product: str, tool_name: str, arguments: dict):
        return {"product": product, "tool": tool_name, "arguments": arguments}


@pytest.mark.asyncio
async def test_server_registers_nine_namespaced_gateway_tools(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_MCP_OAUTH_ISSUER", "https://products.example")
    monkeypatch.setenv("HERMES_MCP_OAUTH_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.setenv("HERMES_MCP_OAUTH_REDIRECT_HOSTS", "claude.ai,claude.com")
    monkeypatch.setenv("HERMES_MCP_OAUTH_STATE_PATH", "/tmp/products-mcp-test-state.json")

    server = build_server(gateway=FakeGateway())
    tools = await server.list_tools()

    assert {tool.name for tool in tools} == EXPECTED_TOOLS


@pytest.mark.asyncio
async def test_status_and_call_tools_return_product_scoped_results(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HERMES_MCP_OAUTH_ISSUER", "https://products.example")
    monkeypatch.setenv("HERMES_MCP_OAUTH_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.setenv("HERMES_MCP_OAUTH_REDIRECT_HOSTS", "claude.ai,claude.com")
    monkeypatch.setenv("HERMES_MCP_OAUTH_STATE_PATH", str(tmp_path / "state.json"))

    server = build_server(gateway=FakeGateway())
    status = await server.call_tool("hypercrawl_status", {})
    called = await server.call_tool(
        "engagemate_call",
        {"tool_name": "account_list", "arguments": {"limit": 2}},
    )

    assert status.structured_content == {
        "product": "hypercrawl",
        "ok": True,
        "toolCount": 1,
    }
    assert called.structured_content == {
        "product": "engagemate",
        "tool": "account_list",
        "arguments": {"limit": 2},
    }
