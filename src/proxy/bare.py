"""Bare mode: strip the Claude Code harness off a failed-over request.

Failover exists so an in-flight session survives a network outage. What it
actually has to survive is not the outage but the *prompt*: a Claude Code
request carries a large system prompt, every tool definition the session has
loaded, and a transcript full of tool results (file reads, grep output,
screenshots). Measured on this machine, an ordinary session with the usual MCP
servers attached reached ~286K tokens of system prompt and tool definitions
alone — before any conversation.

That single number explains the whole history of this ladder. The failover tier
was moved 9B → 4B on 2026-07-09 not because the 9B did not fit in memory, but
because prefilling a context that size took minutes per turn. The context was
treated as fixed and the model was shrunk to cope. Bare mode inverts that: hold
the model constant and shrink the context, which is the term that was actually
out of control.

What gets stripped, in descending order of how much it costs:

  * **Tool definitions.** All of them except an explicit keep-list (Mem0, so
    the failover model can still reach durable memory). A local model cannot
    usefully drive the harness mid-outage anyway.
  * **Tool results in the transcript.** Truncated to a budget. A single Read of
    a large file can outweigh the entire rest of a conversation.
  * **Images.** Replaced by a placeholder. Base64 image data is enormous and
    the failover models are text-only, so this is pure waste.
  * **The system prompt.** Replaced by two sentences of situational context.
    Keeping some orientation beats keeping none: with no system prompt at all a
    model tends to answer as if it were a fresh chat, which reads as a bug.

What is deliberately NOT stripped: the conversation itself. Failing over is
supposed to preserve the session, and the messages are the session.

Scope: this applies ONLY on the failover path. An explicit `/model qwen` route
is a deliberate choice to use a local model *with* the harness, and rewriting
that user's request would be a surprise.
"""

import json
from typing import Any, Iterable

from .models import MessagesRequest, Message

# Substrings matched (case-insensitively) against tool names to decide what
# survives. EMPTY BY DEFAULT — keep no tools at all.
#
# The obvious keep-list is Mem0: durable memory is the one capability worth more
# offline than online. It is also the wrong answer, for two independent reasons
# that both showed up the moment this was tested against a real model.
#
#   1. The Mem0 MCP tools call mcp.mem0.ai. The breaker opens on exactly one
#      condition — this host is offline — so every one of those calls is
#      guaranteed to fail during the only situation where the failover model
#      runs. Keeping them buys a tool that cannot work.
#   2. Not every local model accepts tool definitions at all. deepseek-r1:14b,
#      the current default tier, makes Ollama reject the whole request with
#      "does not support tools" — a 400, i.e. the failover fails outright and
#      the session dies. The exact outcome failover exists to prevent.
#
# Mem0 still reaches the failover model, by the path that works offline: the
# local recall hook reads ~/.mem0-local/cache.db client-side and injects the
# relevant memories into the prompt BEFORE the request is sent. That injection
# is ordinary conversation text, so bare mode preserves it and the model sees it
# whether or not it can call tools.
#
# The mechanism stays configurable (`failover_keep_tools`) because a future tier
# might be a tool-capable model with a genuinely offline tool worth keeping.
DEFAULT_KEEP_TOOLS: tuple[str, ...] = ()

# Per-tool-result character budget. Generous enough to keep a short command's
# output intact, small enough that one `Read` of a big file cannot dominate.
DEFAULT_TOOL_RESULT_CHARS = 2000

# Replaces the harness system prompt. Short on purpose — every token here is
# one the model cannot spend on the conversation.
DEFAULT_SYSTEM = (
    "You are a local model answering inside a Claude Code session that has lost "
    "its network connection, so the usual tools and instructions are gone. "
    "Answer directly from the conversation, keep it brief, and say plainly when "
    "something genuinely needs the network instead of guessing at it."
)


def _matches(name: str, patterns: Iterable[str]) -> bool:
    low = (name or "").lower()
    return any(p.lower() in low for p in patterns)


def parse_keep(raw: str | Iterable[str] | None) -> tuple[str, ...]:
    """Normalize a keep-list from config (comma-separated string or iterable).

    None means "unset, use the default"; an empty string means "explicitly keep
    nothing". Both currently resolve to the same empty tuple, but they must stay
    distinguishable — if DEFAULT_KEEP_TOOLS is ever non-empty again, collapsing
    them would silently ignore an operator who asked for no tools.
    """
    if raw is None:
        return DEFAULT_KEEP_TOOLS
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",")]
    else:
        parts = [str(p).strip() for p in raw]
    kept = tuple(p for p in parts if p)
    return kept or ()


def _truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"\n[… {len(text) - limit} more characters omitted offline]"


def _result_text(content: Any, limit: int) -> str:
    """Flatten a tool_result payload to bounded plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return _truncate(content, limit)
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict):
                if blk.get("type") == "text":
                    parts.append(str(blk.get("text", "")))
                elif blk.get("type") == "image":
                    parts.append("[image omitted offline]")
                else:
                    parts.append(json.dumps(blk)[:200])
            else:
                parts.append(str(blk))
        return _truncate("\n".join(parts), limit)
    return _truncate(str(content), limit)


def _tool_use_names(messages: list[Message]) -> dict[str, str]:
    """Map tool_use id → tool name, so a tool_result can be matched to the tool
    that produced it (a tool_result carries only the id)."""
    out: dict[str, str] = {}
    for m in messages:
        if not isinstance(m.content, list):
            continue
        for blk in m.content:
            if isinstance(blk, dict) and blk.get("type") == "tool_use":
                tid, name = blk.get("id"), blk.get("name")
                if tid and name:
                    out[str(tid)] = str(name)
    return out


def strip_message(
    msg: Message,
    keep: Iterable[str],
    id_to_name: dict[str, str],
    limit: int,
) -> Message:
    """Rewrite one message: keep text, keep kept-tool blocks, reduce the rest."""
    if not isinstance(msg.content, list):
        return msg

    blocks: list[dict[str, Any]] = []
    for blk in msg.content:
        if not isinstance(blk, dict):
            continue
        btype = blk.get("type")

        if btype == "text":
            blocks.append(blk)
        elif btype == "image":
            blocks.append({"type": "text", "text": "[image omitted offline]"})
        elif btype == "tool_use":
            if _matches(str(blk.get("name", "")), keep):
                blocks.append(blk)
            else:
                blocks.append({"type": "text", "text": f"[called {blk.get('name', 'tool')}]"})
        elif btype == "tool_result":
            origin = id_to_name.get(str(blk.get("tool_use_id", "")), "")
            if origin and _matches(origin, keep):
                blocks.append(blk)
            else:
                text = _result_text(blk.get("content"), limit)
                label = f"[{origin or 'tool'} result]"
                blocks.append({"type": "text", "text": f"{label} {text}".rstrip()})
        else:
            blocks.append(blk)

    # When nothing structured survived, collapse to a plain string. Fewer moving
    # parts for the OpenAI translation layer, and it drops the per-block wrapper
    # overhead on exactly the messages that were mostly tool traffic.
    if blocks and all(b.get("type") == "text" for b in blocks):
        joined = "\n".join(str(b.get("text", "")) for b in blocks).strip()
        return Message(role=msg.role, content=joined)

    return Message(role=msg.role, content=blocks)


def make_bare(
    req: MessagesRequest,
    keep: Iterable[str] = DEFAULT_KEEP_TOOLS,
    system: str | None = DEFAULT_SYSTEM,
    tool_result_chars: int = DEFAULT_TOOL_RESULT_CHARS,
) -> MessagesRequest:
    """Return a copy of `req` with the harness stripped off.

    Pure: the caller's request object is not mutated, so a failure here can
    never corrupt the request that gets relayed upstream on recovery.
    """
    keep = tuple(keep)
    id_to_name = _tool_use_names(req.messages)

    bare = req.model_copy(deep=True)
    bare.system = system or None
    bare.messages = [strip_message(m, keep, id_to_name, tool_result_chars) for m in bare.messages]

    if bare.tools:
        kept_tools = [t for t in bare.tools if _matches(t.name, keep)] if keep else []
        bare.tools = kept_tools or None
    if not bare.tools:
        # A tool_choice without tools is a 400 on most backends.
        bare.tool_choice = None

    return bare
