"""Token counting via tiktoken (cl100k_base approximation)."""

import json
from typing import Any

import tiktoken

_enc = tiktoken.get_encoding("cl100k_base")


def count_text(text: str) -> int:
    return len(_enc.encode(text))


def count_messages(
    messages: list[Any],
    system: str | list[dict[str, Any]] | None = None,
    tools: list[Any] | None = None,
) -> int:
    total = 0

    if system:
        if isinstance(system, str):
            total += count_text(system)
        else:
            total += sum(count_text(b.get("text", "")) for b in system if b.get("type") == "text")

    for msg in messages:
        content = msg.content if hasattr(msg, "content") else msg.get("content", "")
        if isinstance(content, str):
            total += count_text(content)
        elif isinstance(content, list):
            for block in content:
                t = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
                if t == "text":
                    total += count_text(block.get("text", "") if isinstance(block, dict) else block.text)
                elif t in ("tool_use", "tool_result"):
                    total += count_text(json.dumps(block))
        total += 4  # per-message overhead

    if tools:
        total += count_text(json.dumps([t.model_dump() if hasattr(t, "model_dump") else t for t in tools]))

    return total
