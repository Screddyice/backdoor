"""Authenticated requests expose saved-session reads without granting mutations."""

import json
import time
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient
from mcp.server.transport_security import TransportSecuritySettings

from src.products_mcp.read_policy import ReadPolicy
from src.products_mcp.server import build_server


@pytest.fixture
def setup(monkeypatch, tmp_path):
    state = tmp_path / "oauth.json"
    tokens = {name: {"token": name, "client_id": client, "scopes": ["hermes"],
        "resource": "https://products.example/mcp", "expires_at": int(time.time()) + 3600}
        for name, client in [("muse", "muse-client"), ("other", "other-client")]}
    state.write_text(json.dumps({"access_tokens": tokens}))
    state.chmod(0o600)
    monkeypatch.setenv("HERMES_MCP_OAUTH_ISSUER", "https://products.example")
    monkeypatch.setenv("HERMES_MCP_OAUTH_PASSWORD", "correct-horse-battery-staple")  # review-gate:allow synthetic fixture, not a live password
    monkeypatch.setenv("HERMES_MCP_OAUTH_REDIRECT_HOSTS", "backend.composio.dev")
    monkeypatch.setenv("HERMES_MCP_OAUTH_STATE_PATH", str(state))
    monkeypatch.setenv("PRODUCTS_MCP_READ_ONLY_CLIENT_IDS", "muse-client")
    gateway = AsyncMock()
    gateway.list_tools.return_value = [{"name": name, "inputSchema": {"type": "object"}}
        for name in ["post_v1_search", "post_v1_crawl_public", "post_v1_crawl", "get_v1_sessions", "post_v1_accounts"]]
    gateway.call_tool.return_value = {"ok": True}
    server = build_server(product="hypercrawl", gateway=gateway)
    with TestClient(server.streamable_http_app(stateless_http=True, json_response=True,
                    transport_security=TransportSecuritySettings(allowed_hosts=["products.example"])),
                    base_url="https://products.example") as client:
        def call(name, arguments=None, token="muse"):
            response = client.post("/mcp", headers={"Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 1,
                "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}})
            return response
        yield gateway, call


def test_authenticated_catalog_and_status_expose_only_authorized_operations(setup):
    gateway, call = setup
    result = call("hypercrawl_list_tools").json()["result"]["structuredContent"]
    assert [t["name"] for t in result["tools"]] == ["post_v1_search", "post_v1_crawl_public", "post_v1_crawl", "get_v1_sessions"]
    assert result["tools"][0]["inputSchema"]["additionalProperties"] is False
    assert result["tools"][-1]["inputSchema"] == {"type": "object", "additionalProperties": False, "properties": {}}
    assert call("hypercrawl_status").json()["result"]["structuredContent"]["toolCount"] == 4
    gateway.call_tool.assert_not_awaited()


@pytest.mark.parametrize("name,body,expected", [
    ("post_v1_search", {"query": " IANA ", "maxResults": 1},
     {"query": "IANA", "maxResults": 1, "provider": "searxng", "cache": False}),
    ("post_v1_crawl_public", {"url": "https://example.com/"}, {"url": "https://example.com/"}),
    ("post_v1_crawl", {"url": "https://example.com/profile"}, {"url": "https://example.com/profile"}),
])
def test_exact_reads_dispatch_bounded_input(setup, name, body, expected):
    gateway, call = setup
    result = call("hypercrawl_call", {"tool_name": name, "arguments": {"body": body}})
    assert result.json()["result"].get("isError") is not True
    gateway.call_tool.assert_awaited_once_with("hypercrawl", name, {"body": expected})


@pytest.mark.parametrize("name,args", [
    ("post_v1_accounts", {"body": {}}), ("post_v1_sessions", {"body": {}}),
    ("delete_v1_sessions", {"body": {"domain": "example.com"}}),
    ("get_v1_sessions", {"tenant": "other"}),
    ("get_v1_sessions", {"body": {}}),
    ("post_v1_crawl", {"body": {"url": "https://example.com", "actions": []}}),
    ("post_v1_crawl", {"body": {"url": "https://example.com", "storageState": {}}}),
    ("post_v1_crawl", {"body": {"url": "https://user:password@example.com"}}),
    ("POST_V1_SEARCH", {"body": {"query": "IANA"}}),
    ("post_v1_search", {"body": {"query": "IANA", "maxResults": 6}}),
    ("post_v1_search", {"body": {"query": "IANA", "maxResults": True}}),
    ("post_v1_search", {"body": {"query": " "}}),
    ("post_v1_search", {"body": {"query": "IANA", "provider": "bing"}}),
    ("post_v1_search", {"body": {"query": "IANA", "cache": True}}),
    ("post_v1_search", {"body": {"query": "IANA", "cache": 0}}),
    ("post_v1_search", {"body": {"query": "IANA", "actions": []}}),
    ("post_v1_search", {"body": {"query": "IANA"}, "headers": {}}),
    ("post_v1_crawl_public", {"body": {"url": "https://example.com", "actions": []}}),
    ("post_v1_crawl_public", {"body": {"url": "file:///tmp/x"}}),
    ("post_v1_crawl_public", {"body": {"url": "https://example.com/\n"}}),
])
def test_disallowed_calls_stop_before_any_upstream_dispatch(setup, name, args):
    gateway, call = setup
    result = call("hypercrawl_call", {"tool_name": name, "arguments": args}).json()
    assert result["result"]["isError"] is True
    gateway.call_tool.assert_not_awaited()
    gateway.list_tools.assert_not_awaited()


def test_other_authenticated_clients_keep_existing_surface(setup):
    gateway, call = setup
    result = call("hypercrawl_list_tools", token="other").json()["result"]["structuredContent"]
    assert len(result["tools"]) == 5
    call("hypercrawl_call", {"tool_name": "post_v1_accounts", "arguments": {"body": {}}}, token="other")
    gateway.call_tool.assert_awaited_once_with("hypercrawl", "post_v1_accounts", {"body": {}})


def test_saved_session_metadata_dispatches_without_cookie_or_tenant_arguments(setup):
    gateway, call = setup
    result = call("hypercrawl_call", {"tool_name": "get_v1_sessions", "arguments": {}}).json()
    assert result["result"].get("isError") is not True
    gateway.call_tool.assert_awaited_once_with("hypercrawl", "get_v1_sessions", {})


def test_invalid_auth_is_rejected_before_gateway(setup):
    gateway, call = setup
    assert call("hypercrawl_status", token="invalid").status_code == 401
    gateway.list_tools.assert_not_awaited()


def test_configured_policy_without_verified_context_fails_closed():
    with pytest.raises(PermissionError, match="Authenticated"):
        ReadPolicy(frozenset({"muse-client"})).arguments("post_v1_search", {"body": {"query": "IANA"}})


def test_invalid_or_wrong_product_policy_fails_at_startup(monkeypatch):
    monkeypatch.setenv("PRODUCTS_MCP_READ_ONLY_CLIENT_IDS", "muse-client")
    with pytest.raises(ValueError):
        ReadPolicy.from_env("hyperscale")
    monkeypatch.setenv("PRODUCTS_MCP_READ_ONLY_CLIENT_IDS", "bad client")
    with pytest.raises(ValueError):
        ReadPolicy.from_env("hypercrawl")
