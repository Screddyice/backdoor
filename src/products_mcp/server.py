"""OAuth-protected remote MCP server for three product gateways."""

from __future__ import annotations

import asyncio
import os
from typing import Any, cast

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import MCPServer

from src.hermes_mcp.oauth import (
    LOGIN_COMPLETION_PATH,
    OAuthSettings,
    SingleUserOAuthProvider,
)

from .client import ProductGateway, ProductName, ProductSettings


PRODUCT_NAMES = ("hypercrawl", "hyperscale", "engagemate")


def _selected_product(product: str | None = None) -> ProductName:
    selected = (product or os.environ.get("PRODUCTS_MCP_PRODUCT", "")).strip().lower()
    if not selected:
        raise ValueError(
            "PRODUCTS_MCP_PRODUCT must select hypercrawl, hyperscale, or engagemate"
        )
    if selected not in PRODUCT_NAMES:
        raise ValueError(f"unknown product: {selected}")
    return cast(ProductName, selected)


def build_server(
    *, product: str | None = None, gateway: ProductGateway | None = None
) -> MCPServer:
    selected_product = _selected_product(product)
    oauth_settings = OAuthSettings.from_env()
    provider = SingleUserOAuthProvider(oauth_settings)
    server = MCPServer(
        selected_product,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=oauth_settings.issuer,
            resource_server_url=oauth_settings.resource_url,
            required_scopes=[oauth_settings.scope],
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=[oauth_settings.scope],
                default_scopes=[oauth_settings.scope],
            ),
        ),
    )

    @server.custom_route("/login", methods=["GET", "POST"])
    async def oauth_login(request):
        return await provider.handle_login(request)

    @server.custom_route(LOGIN_COMPLETION_PATH, methods=["GET"])
    async def oauth_login_complete(request):
        return await provider.handle_login_completion(request)

    active_gateway = gateway or ProductGateway(
        ProductSettings.from_env(selected_product)
    )
    _register_product(server, active_gateway, selected_product)
    return server


def _register_product(
    server: MCPServer, gateway: ProductGateway, product: ProductName
) -> None:
    @server.tool(
        name=f"{product}_list_tools",
        description=f"List the callable {product} operations and their input schemas.",
    )
    async def list_tools() -> dict[str, Any]:
        tools = await gateway.list_tools(product)
        return {"product": product, "tools": tools}

    @server.tool(
        name=f"{product}_call",
        description=f"Call one advertised {product} operation by name.",
    )
    async def call_tool(
        tool_name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await gateway.call_tool(product, tool_name, arguments or {})

    @server.tool(
        name=f"{product}_status",
        description=f"Check {product} connectivity and report its current tool count.",
    )
    async def status() -> dict[str, Any]:
        tools = await gateway.list_tools(product)
        return {"product": product, "ok": True, "toolCount": len(tools)}


def main() -> None:
    from src.hermes_mcp.http_server import (
        _host_from_env,
        _port_from_env,
        _transport_security,
        require_host_allowlist,
    )

    host = _host_from_env()
    require_host_allowlist(host)
    asyncio.run(
        build_server().run_streamable_http_async(
            host=host,
            port=_port_from_env(),
            transport_security=_transport_security(),
        )
    )


if __name__ == "__main__":
    main()
