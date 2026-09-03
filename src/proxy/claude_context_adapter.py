"""Normalize and rebuild exact Anthropic Messages request conversation data."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from .context_segments import (
    ContextSegment,
    NormalizedContext,
    canonical_json,
    segment_identifier,
    selected_with_pairs,
)
from .models import Message, MessagesRequest


def _text_from_tool_result(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(item["text"])
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
    return ""


def _searchable_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, dict):
        return ""
    kind = content.get("type")
    if kind == "text" and isinstance(content.get("text"), str):
        return content["text"]
    if kind == "tool_use":
        parts = [str(content.get("name") or "")]
        if "input" in content:
            parts.append(canonical_json(content["input"]))
        return "\n".join(part for part in parts if part)
    if kind == "tool_result":
        return _text_from_tool_result(content.get("content"))
    return ""


def _kind(content: Any) -> str:
    if isinstance(content, str):
        return "text"
    if isinstance(content, dict) and isinstance(content.get("type"), str):
        return content["type"]
    return "content"


def _pair_id(content: Any) -> str | None:
    if not isinstance(content, dict):
        return None
    if content.get("type") == "tool_use":
        return str(content["id"]) if content.get("id") else None
    if content.get("type") == "tool_result":
        return str(content["tool_use_id"]) if content.get("tool_use_id") else None
    return None


def _user_text(content: str | list[dict[str, Any]]) -> tuple[int, str] | None:
    if isinstance(content, str):
        return (0, content) if content.strip() else None
    for index in range(len(content) - 1, -1, -1):
        block = content[index]
        if (
            isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
            and block["text"].strip()
        ):
            return index, block["text"]
    return None


class ClaudeContextAdapter:
    def normalize(self, req: MessagesRequest) -> NormalizedContext:
        latest_user_index: int | None = None
        for index in range(len(req.messages) - 1, -1, -1):
            if req.messages[index].role == "user":
                latest_user_index = index
                break
        if latest_user_index is None:
            raise ValueError("Claude request has no textual current user instruction")

        current_content_index = _user_text(req.messages[latest_user_index].content)
        if current_content_index is None:
            raise ValueError("Claude request has no textual current user instruction")

        segments: list[ContextSegment] = []
        current_segment_id: str | None = None
        ordinal = 0
        for message_index, message in enumerate(req.messages):
            parts: list[Any]
            if isinstance(message.content, list):
                parts = list(message.content) or [message.content]
            else:
                parts = [message.content]
            for content_index, content in enumerate(parts):
                kind = _kind(content)
                exact = {"role": message.role, "content": content}
                segment = ContextSegment(
                    segment_id=segment_identifier("claude", message.role, kind, content),
                    ordinal=ordinal,
                    role=message.role,
                    kind=kind,
                    exact_json=canonical_json(exact),
                    searchable_text=_searchable_text(content),
                    pair_id=_pair_id(content),
                )
                segments.append(segment)
                if (
                    message_index == latest_user_index
                    and content_index == current_content_index[0]
                ):
                    current_segment_id = segment.segment_id
                ordinal += 1

        if current_segment_id is None:  # Defensive: the validation above found it.
            raise ValueError("Claude request has no textual current user instruction")
        return NormalizedContext(
            client_kind="claude",
            model=req.model,
            segments=tuple(segments),
            current_segment_id=current_segment_id,
            native=req.model_copy(deep=True),
        )

    def rebuild(
        self,
        context: NormalizedContext,
        selected_ids: Collection[str],
        historical_text: str | None = None,
    ) -> MessagesRequest:
        if context.client_kind != "claude" or not isinstance(context.native, MessagesRequest):
            raise ValueError("ClaudeContextAdapter requires Claude normalized context")
        del historical_text  # Retrieval formatting is applied by the later selector.
        selected = selected_with_pairs(context.segments, selected_ids)
        rebuilt = context.native.model_copy(deep=True)
        rebuilt_messages: list[Message] = []
        ordinal = 0
        for message in context.native.messages:
            if isinstance(message.content, list):
                source = list(message.content)
                if not source:
                    if context.segments[ordinal].segment_id in selected:
                        rebuilt_messages.append(Message(role=message.role, content=[]))
                    ordinal += 1
                    continue
                kept = []
                for content in source:
                    if context.segments[ordinal].segment_id in selected:
                        kept.append(content)
                    ordinal += 1
                if kept:
                    rebuilt_messages.append(Message(role=message.role, content=kept))
            else:
                if context.segments[ordinal].segment_id in selected:
                    rebuilt_messages.append(Message(role=message.role, content=message.content))
                ordinal += 1
        rebuilt.messages = rebuilt_messages
        return rebuilt
