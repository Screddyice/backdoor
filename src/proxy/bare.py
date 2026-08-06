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

# What survives from the tool list. Entries are substrings matched
# case-insensitively against tool names, plus one special token:
#
#   "local"  keep every tool whose name is NOT prefixed `mcp__`
#
# LOCAL BY DEFAULT, and the split is the useful one. During an outage the
# harness's own tools still work perfectly — Read, Edit, Bash, Glob and Grep
# touch nothing but this disk — so keeping them lets the failover model carry on
# doing work instead of only talking about it. Every `mcp__*` tool is a remote
# integration and is dead for exactly as long as the breaker is open, since the
# breaker opens on one condition only: this host is offline.
#
# That also happens to be where the weight is. The ~286K tokens of definitions
# measured on this machine came from MCP servers (apify, atlassian, nebos,
# hostinger, composio), not from the dozen local tools, so dropping `mcp__*`
# removes nearly all of the cost and nearly none of the offline capability.
#
# Mem0 is the interesting case and it belongs on the dropped side. Its MCP tools
# call mcp.mem0.ai and cannot work offline, but local Mem0 recall still reaches
# the model anyway: the hook reads ~/.mem0-local/cache.db client-side and
# injects memories into the prompt BEFORE the request is sent, which bare mode
# preserves as ordinary text.
#
# REQUIRED: the failover tier must accept tool definitions. deepseek-r1 does
# not, and Ollama answers a request carrying them with HTTP 400 ("does not
# support tools"), killing the session failover exists to save. Pair a non-empty
# keep-list only with a tool-capable model, and set it to "" for one that is not.
DEFAULT_KEEP_TOOLS: tuple[str, ...] = ("local",)

# Tools from an MCP server carry this prefix. Everything else is harness-local.
MCP_PREFIX = "mcp__"

# The special keep-list token meaning "everything that is not an MCP tool".
LOCAL_TOKEN = "local"

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
    """Does this tool name survive the keep-list?

    `LOCAL_TOKEN` is checked as a prefix rule rather than a substring, so a
    remote tool that merely contains the word (`mcp__local_files__read`) is
    still dropped. Substring matching is fine for the explicit entries, which
    an operator writes deliberately.
    """
    low = (name or "").lower()
    for p in patterns:
        p = p.lower()
        if p == LOCAL_TOKEN:
            if not low.startswith(MCP_PREFIX):
                return True
        elif p in low:
            return True
    return False


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
            # Truncate the payload for EVERY tool, kept or not. The keep-list
            # decides which tools the model may still CALL; it must not decide
            # how much history they drag along. A kept tool's definition costs a
            # few hundred tokens, while one of its past results — a Read of a
            # large file, a verbose Bash run — can outweigh the whole
            # conversation. Letting the keep-list govern both put the harness
            # bloat straight back in through the transcript.
            origin = id_to_name.get(str(blk.get("tool_use_id", "")), "")
            text = _result_text(blk.get("content"), limit)
            if origin and _matches(origin, keep):
                # Kept tool: preserve the block's structure (the model may be
                # mid-loop and needs the tool_use_id to line up), trimmed body.
                trimmed = dict(blk)
                trimmed["content"] = text
                blocks.append(trimmed)
            else:
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
