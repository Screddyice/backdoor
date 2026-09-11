"""Normalize and rebuild exact OpenAI Responses request conversation data."""

from __future__ import annotations

from collections.abc import Collection
import copy
from typing import Any

from .context_segments import (
    ContextSegment,
    NormalizedContext,
    canonical_json,
    content_hash,
    segment_identifier,
    selected_with_pairs,
)


def _message_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") in {"input_text", "output_text", "text"}
        and isinstance(block.get("text"), str)
    )


def _role(item: Any) -> str:
    if not isinstance(item, dict):
        return "unknown"
    if isinstance(item.get("role"), str):
        return item["role"]
    if item.get("type") == "function_call":
        return "assistant"
    if item.get("type") == "function_call_output":
        return "tool"
    return "unknown"


def _kind(item: Any) -> str:
    if isinstance(item, dict) and isinstance(item.get("type"), str):
        return item["type"]
    return "input"


def _pair_id(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    if item.get("type") in {"function_call", "function_call_output"}:
        return str(item["call_id"]) if item.get("call_id") else None
    return None


def _searchable_text(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    kind = item.get("type")
    if kind == "message":
        return _message_text(item)
    if kind == "function_call":
        parts = [str(item.get("name") or ""), str(item.get("arguments") or "")]
        return "\n".join(part for part in parts if part)
    if kind == "function_call_output":
        output = item.get("output")
        if isinstance(output, str):
            return output
        if output is not None:
            return canonical_json(output)
    return ""


class CodexContextAdapter:
    def normalize(self, payload: dict[str, Any]) -> NormalizedContext:
        items = payload.get("input")
        if not isinstance(items, list):
            raise ValueError("Codex request input must be an array")

        latest_user_index: int | None = None
        for index in range(len(items) - 1, -1, -1):
            item = items[index]
            if isinstance(item, dict) and item.get("role") == "user":
                latest_user_index = index
                break
        if latest_user_index is None or not _message_text(items[latest_user_index]).strip():
            raise ValueError("Codex request has no textual current user instruction")

        segments: list[ContextSegment] = []
        for ordinal, item in enumerate(items):
            role = _role(item)
            kind = _kind(item)
            segments.append(
                ContextSegment(
                    segment_id=segment_identifier("codex", role, kind, item, ordinal),
                    content_hash=content_hash("codex", role, kind, item),
                    ordinal=ordinal,
                    role=role,
                    kind=kind,
                    exact_json=canonical_json(item),
                    searchable_text=_searchable_text(item),
                    pair_id=_pair_id(item),
                )
            )
        return NormalizedContext(
            client_kind="codex",
            model=str(payload.get("model") or ""),
            segments=tuple(segments),
            current_segment_id=segments[latest_user_index].segment_id,
            native=copy.deepcopy(payload),
        )

    def rebuild(
        self,
        context: NormalizedContext,
        selected_ids: Collection[str],
        historical_text: str | None = None,
    ) -> dict[str, Any]:
        if context.client_kind != "codex" or not isinstance(context.native, dict):
            raise ValueError("CodexContextAdapter requires Codex normalized context")
        del historical_text  # Retrieval formatting is applied by the later selector.
        selected = selected_with_pairs(context.segments, selected_ids)
        rebuilt = copy.deepcopy(context.native)
        rebuilt["input"] = [
            copy.deepcopy(item)
            for segment, item in zip(context.segments, context.native["input"], strict=True)
            if segment.segment_id in selected
        ]
        return rebuilt
