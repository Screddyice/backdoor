"""Anthropic Messages API request/response models."""

from typing import Any, Literal
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Content blocks
# ---------------------------------------------------------------------------

class TextBlock(BaseModel):
    type: Literal["text"]
    text: str


class ImageSource(BaseModel):
    type: Literal["base64", "url"]
    media_type: str | None = None
    data: str | None = None
    url: str | None = None


class ImageBlock(BaseModel):
    type: Literal["image"]
    source: ImageSource


class ToolUseBlock(BaseModel):
    type: Literal["tool_use"]
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultBlock(BaseModel):
    type: Literal["tool_result"]
    tool_use_id: str
    content: str | list[dict[str, Any]] | None = None
    is_error: bool = False


ContentBlock = TextBlock | ImageBlock | ToolUseBlock | ToolResultBlock


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str | list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

class Tool(BaseModel):
    name: str
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)


class ToolChoice(BaseModel):
    type: Literal["auto", "any", "tool", "none"] = "auto"
    name: str | None = None


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------

class MessagesRequest(BaseModel):
    model: str
    messages: list[Message]
    system: str | list[dict[str, Any]] | None = None
    max_tokens: int = 8192
    stream: bool = False
    tools: list[Tool] | None = None
    tool_choice: ToolChoice | None = None
    temperature: float | None = None
    top_p: float | None = None
    stop_sequences: list[str] | None = None
    metadata: dict[str, Any] | None = None


class TokenCountRequest(BaseModel):
    model: str
    messages: list[Message]
    system: str | list[dict[str, Any]] | None = None
    tools: list[Tool] | None = None


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------

class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class MessagesResponse(BaseModel):
    id: str
    type: str = "message"
    role: str = "assistant"
    model: str
    content: list[dict[str, Any]] = Field(default_factory=list)
    stop_reason: str | None = None
    stop_sequence: str | None = None
    usage: Usage = Field(default_factory=Usage)


class TokenCountResponse(BaseModel):
    input_tokens: int
