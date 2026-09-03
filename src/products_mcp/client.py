"""Upstream clients for the account-synced products MCP gateway."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Any, Literal
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

ProductName = Literal["hypercrawl", "hyperscale", "engagemate"]


class UpstreamError(RuntimeError):
    """An upstream product request failed safely."""


@dataclass(frozen=True)
class ProductSettings:
    hypercrawl_url: str
    hypercrawl_token: str
    hyperscale_url: str
    hyperscale_key: str
    engagemate_url: str
    engagemate_key: str
    engagemate_user_id: str

    @classmethod
    def from_env(cls, product: ProductName | None = None) -> "ProductSettings":
        values = cls(
            hypercrawl_url=os.environ.get("HYPERCRAWL_URL", "").rstrip("/"),
            hypercrawl_token=os.environ.get("HYPERCRAWL_REST_TOKEN", ""),
            hyperscale_url=os.environ.get(
                "HYPERSCALE_MCP_URL",
                "https://hyperflow-one--teamnebula-ai.us-central1.hosted.app/mcp",
            ).rstrip("/"),
            hyperscale_key=os.environ.get("HYPERFLOW_API_KEY", ""),
            engagemate_url=os.environ.get(
                "ENGAGEMATE_URL", "http://127.0.0.1:13100"
            ).rstrip("/"),
            engagemate_key=os.environ.get("ENGAGEMATE_INTERNAL_API_KEY", "")
            or os.environ.get("INTERNAL_API_KEY", ""),
            engagemate_user_id=os.environ.get("ENGAGEMATE_USER_ID", ""),
        )
        required_by_product = {
            "hypercrawl": {"HYPERCRAWL_URL", "HYPERCRAWL_REST_TOKEN"},
            "hyperscale": {"HYPERFLOW_API_KEY"},
            "engagemate": {"INTERNAL_API_KEY", "ENGAGEMATE_USER_ID"},
        }
        required = (
            required_by_product[product]
            if product is not None
            else set().union(*required_by_product.values())
        )
        missing = [
            name
            for name, value in {
                "HYPERCRAWL_URL": values.hypercrawl_url,
                "HYPERCRAWL_REST_TOKEN": values.hypercrawl_token,
                "HYPERFLOW_API_KEY": values.hyperscale_key,
                "INTERNAL_API_KEY": values.engagemate_key,
                "ENGAGEMATE_USER_ID": values.engagemate_user_id,
            }.items()
            if name in required and not value
        ]
        if missing:
            raise ValueError(f"missing product gateway settings: {', '.join(missing)}")
        return values


@dataclass(frozen=True)
class _Operation:
    method: str
    path: str
    path_parameters: tuple[str, ...]
    query_parameters: tuple[str, ...]
    has_body: bool
    body_required: bool
    tool: dict[str, Any]


class ProductGateway:
    def __init__(
        self,
        settings: ProductSettings,
        *,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(60.0))
        self._operations: dict[ProductName, dict[str, _Operation]] = {}

    async def list_tools(self, product: ProductName) -> list[dict[str, Any]]:
        if product == "hyperscale":
            result = await self._hyperscale_rpc("tools/list", {})
            tools = result.get("tools")
            if not isinstance(tools, list):
                raise UpstreamError("hyperscale returned an invalid tool catalog")
            return tools

        operations = await self._openapi_operations(product)
        return [operation.tool for operation in operations.values()]

    async def call_tool(
        self,
        product: ProductName,
        tool_name: str,
        arguments: dict[str, Any] | None,
    ) -> Any:
        arguments = arguments or {}
        if product == "hyperscale":
            return await self._hyperscale_rpc(
                "tools/call", {"name": tool_name, "arguments": arguments}
            )

        operations = await self._openapi_operations(product)
        operation = operations.get(tool_name)
        if operation is None:
            raise UpstreamError(f"{product} tool is not advertised: {tool_name}")

        allowed = {
            *operation.path_parameters,
            *operation.query_parameters,
            *({"body"} if operation.has_body else set()),
        }
        unexpected = sorted(set(arguments) - allowed)
        if unexpected:
            raise UpstreamError(
                f"{product} tool received unexpected argument: {unexpected[0]}"
            )
        if operation.body_required and "body" not in arguments:
            raise UpstreamError(f"{product} tool requires request body")

        path = operation.path
        for name in operation.path_parameters:
            if name not in arguments:
                raise UpstreamError(f"{product} tool requires path argument: {name}")
            path = path.replace(f"{{{name}}}", quote(str(arguments[name]), safe=""))
        query = {
            name: arguments[name]
            for name in operation.query_parameters
            if name in arguments
        }
        body = arguments.get("body") if operation.has_body else None
        base_url, headers = self._rest_connection(product)
        return await self._request_json(
            product,
            operation.method,
            self._operation_url(base_url, path),
            headers=headers,
            params=query,
            json_body=body,
        )

    def _rest_connection(self, product: ProductName) -> tuple[str, dict[str, str]]:
        if product == "hypercrawl":
            return self.settings.hypercrawl_url, {
                "authorization": f"Bearer {self.settings.hypercrawl_token}"
            }
        if product == "engagemate":
            return self.settings.engagemate_url, {
                "x-internal-api-key": self.settings.engagemate_key,
                "x-user-id": self.settings.engagemate_user_id,
            }
        raise UpstreamError(f"unknown product: {product}")

    async def _openapi_operations(
        self, product: ProductName
    ) -> dict[str, _Operation]:
        cached = self._operations.get(product)
        if cached is not None:
            return cached
        base_url, headers = self._rest_connection(product)
        response = await self._request_json(
            product, "GET", f"{base_url}/openapi.json", headers=headers
        )
        document = response["body"]
        if not isinstance(document, dict):
            raise UpstreamError(f"{product} returned an invalid OpenAPI document")
        operations: dict[str, _Operation] = {}
        for path, path_item in (document.get("paths") or {}).items():
            if not isinstance(path_item, dict):
                continue
            for method, definition in path_item.items():
                if method.upper() not in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
                    continue
                if not isinstance(definition, dict):
                    continue
                name = definition.get("operationId")
                if not isinstance(name, str) or not name:
                    flattened = path.strip("/").replace("/", "_").replace("-", "_")
                    name = f"{method.lower()}_{flattened}"
                parameters = definition.get("parameters") or []
                path_parameters = tuple(
                    item["name"]
                    for item in parameters
                    if isinstance(item, dict)
                    and item.get("in") == "path"
                    and isinstance(item.get("name"), str)
                )
                query_parameters = tuple(
                    item["name"]
                    for item in parameters
                    if isinstance(item, dict)
                    and item.get("in") == "query"
                    and isinstance(item.get("name"), str)
                )
                properties = {
                    item["name"]: item.get("schema", {"type": "string"})
                    for item in parameters
                    if isinstance(item, dict) and isinstance(item.get("name"), str)
                }
                required = [
                    item["name"]
                    for item in parameters
                    if isinstance(item, dict)
                    and item.get("required") is True
                    and isinstance(item.get("name"), str)
                ]
                request_body = definition.get("requestBody")
                has_body = isinstance(request_body, dict)
                body_required = has_body and request_body.get("required") is True
                if has_body:
                    schema = (
                        request_body.get("content", {})
                        .get("application/json", {})
                        .get("schema", {"type": "object"})
                    )
                    properties["body"] = schema
                    if body_required:
                        required.append("body")
                tool = {
                    "name": name,
                    "description": str(
                        definition.get("summary")
                        or definition.get("description")
                        or f"{method.upper()} {path}"
                    )[:500],
                    "inputSchema": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                }
                operations[name] = _Operation(
                    method=method.upper(),
                    path=path,
                    path_parameters=path_parameters,
                    query_parameters=query_parameters,
                    has_body=has_body,
                    body_required=body_required,
                    tool=tool,
                )
        self._operations[product] = operations
        return operations

    @staticmethod
    def _operation_url(base_url: str, path: str) -> str:
        parsed = urlsplit(base_url)
        base_path = parsed.path.rstrip("/")
        if base_path and (path == base_path or path.startswith(f"{base_path}/")):
            target_path = path
        else:
            target_path = f"{base_path}/{path.lstrip('/')}"
        return urlunsplit((parsed.scheme, parsed.netloc, target_path, "", ""))

    async def _hyperscale_rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "x-api-key": self.settings.hyperscale_key,
        }
        message = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            response = await self.http.post(
                self.settings.hyperscale_url, headers=headers, json=message
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(f"hyperscale request failed: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise UpstreamError(
                f"hyperscale upstream returned HTTP {response.status_code}"
            )
        payload = self._mcp_payload(response)
        if "error" in payload:
            error = payload.get("error") or {}
            code = error.get("code", "unknown") if isinstance(error, dict) else "unknown"
            raise UpstreamError(f"hyperscale MCP returned error code {code}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise UpstreamError("hyperscale returned an invalid MCP response")
        return result

    @staticmethod
    def _mcp_payload(response: httpx.Response) -> dict[str, Any]:
        if "text/event-stream" in response.headers.get("content-type", ""):
            for line in response.text.splitlines():
                if line.startswith("data:"):
                    value = json.loads(line[5:].strip())
                    if isinstance(value, dict):
                        return value
            raise UpstreamError("hyperscale returned an empty MCP event stream")
        value = response.json()
        if not isinstance(value, dict):
            raise UpstreamError("hyperscale returned a non-object MCP response")
        return value

    async def _request_json(
        self,
        product: ProductName,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> dict[str, Any]:
        try:
            response = await self.http.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
            )
        except httpx.HTTPError as exc:
            raise UpstreamError(f"{product} request failed: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise UpstreamError(
                f"{product} upstream returned HTTP {response.status_code}"
            )
        try:
            body: Any = response.json()
        except ValueError:
            body = response.text
        return {"status": response.status_code, "body": body}
