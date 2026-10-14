"""Bound selected OAuth clients to explicit reads before upstream dispatch."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from mcp.server.auth.middleware.auth_context import get_access_token
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, field_validator


class PublicSearch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=500)
    maxResults: StrictInt = Field(default=5, ge=1, le=5)
    context: str | None = Field(default=None, max_length=2000)
    provider: Literal["searxng"] = "searxng"
    cache: Literal[False] = False

    @field_validator("cache", mode="before")
    @classmethod
    def cache_is_false(cls, value: Any) -> bool:
        if value is not False:
            raise ValueError("cache must be false")
        return False

    @field_validator("query")
    @classmethod
    def query_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value.strip()


class PublicCrawl(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=2048)

    @field_validator("url")
    @classmethod
    def anonymous_http_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or any(char.isspace() or ord(char) < 32 for char in value)):
            raise ValueError("url must be an anonymous HTTP URL")
        return value


READ_INPUTS = {
    "post_v1_search": PublicSearch,
    "post_v1_crawl_public": PublicCrawl,
    # This URL-only REST operation applies the existing tenant's saved session;
    # it accepts no browser actions, inline cookies, or caller-selected tenant.
    "post_v1_crawl": PublicCrawl,
}
SESSION_METADATA = "get_v1_sessions"


@dataclass(frozen=True)
class ReadPolicy:
    client_ids: frozenset[str]

    @classmethod
    def from_env(cls, product: str) -> "ReadPolicy":
        raw = os.environ.get("PRODUCTS_MCP_READ_ONLY_CLIENT_IDS", "")
        clients = frozenset(item.strip() for item in raw.split(",") if item.strip())
        if clients and (product != "hypercrawl" or any(
            re.fullmatch(r"[A-Za-z0-9_-]{1,128}", item) is None for item in clients
        )):
            raise ValueError("Invalid HyperCrawl read-only OAuth client policy")
        return cls(clients)

    def restricted(self) -> bool:
        if not self.client_ids:
            return False
        token = get_access_token()
        if token is None:
            raise PermissionError("Authenticated OAuth client required")
        return token.client_id in self.client_ids

    def catalog(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self.restricted():
            return tools
        result = [{**tool, "inputSchema": {
            "type": "object", "additionalProperties": False,
            "properties": {"body": READ_INPUTS[tool["name"]].model_json_schema()},
            "required": ["body"],
        }} for tool in tools if tool.get("name") in READ_INPUTS]
        result.extend({**tool, "inputSchema": {
            "type": "object", "additionalProperties": False, "properties": {},
        }} for tool in tools if tool.get("name") == SESSION_METADATA)
        return result

    def arguments(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.restricted():
            return arguments
        if name == SESSION_METADATA:
            if arguments:
                raise ValueError("Session metadata operation accepts no arguments")
            return {}
        model = READ_INPUTS.get(name)
        if model is None:
            raise PermissionError("Operation is not allowed for this read-only client")
        if set(arguments) != {"body"} or not isinstance(arguments["body"], dict):
            raise ValueError("Read-only operation requires only a body object")
        try:
            body = model.model_validate(arguments["body"], strict=True)
        except ValidationError:
            # ValidationError includes the caller's input; do not echo URLs or payloads.
            raise ValueError("Invalid read-only operation input") from None
        return {"body": body.model_dump(exclude_none=True)}
