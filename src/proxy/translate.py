"""Translate between Anthropic Messages API format and OpenAI/NIM format."""

import json
import logging
import re
import uuid
from typing import Any

from .models import MessagesRequest, Message, Tool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TOOL_CALL_TAG_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _new_tool_use_id() -> str:
    return f"toolu_{uuid.uuid4().hex[:24]}"


def looks_like_tool_call_start(buf: str) -> bool:
    """Cheap check: could this accumulating text be a content-embedded tool call?

    Some models (e.g. qwen2.5-coder) ignore the <tool_call> wrapper and emit the
    raw JSON object as plain content, so Ollama never populates `tool_calls`.
    We buffer such output and convert it; normal prose/code starts with neither
    `{` nor a <tool_call> tag, so it streams through untouched.
    """
    s = buf.lstrip()
    if not s:
        return False
    return s[0] == "{" or s.startswith("<tool_call")


def extract_text_tool_calls(text: str, tool_names: set[str]) -> list[dict[str, Any]] | None:
    """Parse content-embedded tool calls into [{name, input}], or None.

    Handles three shapes a model may emit as plain text instead of structured
    tool_calls: bare `{"name":..., "arguments":...}`, the same wrapped in
    <tool_call></tool_call> tags, and the same inside a ```json fence.
    A candidate only counts if its name matches a tool offered in the request —
    this stops a legitimate JSON answer from being misread as a tool call.
    """
    if not text or not tool_names:
        return None

    raw = text.strip()
    candidates: list[str] = []

    tagged = _TOOL_CALL_TAG_RE.findall(raw)
    if tagged:
        candidates = tagged
    else:
        candidates = [_JSON_FENCE_RE.sub("", raw).strip()]

    calls: list[dict[str, Any]] = []
    for cand in candidates:
        cand = cand.strip()
        if not cand or cand[0] != "{":
            continue
        try:
            obj = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            return None  # malformed — don't risk a false positive
        if not isinstance(obj, dict):
            return None
        name = obj.get("name")
        args = obj.get("arguments")
        if args is None:
            args = obj.get("parameters")
        if name not in tool_names:
            return None
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except (json.JSONDecodeError, ValueError):
                args = {}
        if not isinstance(args, dict):
            args = {}
        calls.append({"name": name, "input": args})

    return calls or None


THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"


def split_inline_thinking(text: str) -> tuple[str, str]:
    """Split inline <think> blocks out of content. Returns (thinking, text).

    Ollama reports reasoning in a separate `reasoning` field, which both response
    paths already handle. mlx_vlm.server does not: it leaves the tags inline in
    `content`, so a `qwen` turn rendered the model's reasoning, a bare
    `</think>`, and then the real answer. PROVIDER_REASONING_EFFORT=none does not
    help, because that is an Ollama-ism the MLX server ignores.

    Three shapes, and the second is the common one:

    1. `<think>reasoning</think>answer` — both tags present.
    2. `reasoning</think>answer` — closer only. Qwen's chat template pre-fills
       the opening `<think>` into the assistant turn, so the model never emits
       it and the content STARTS inside the block. This is what leaked.
    3. `<think>reasoning` — unterminated, usually a max_tokens cutoff.

    Thinking is extracted rather than deleted, matching how the `reasoning`
    field is treated: a reasoning-only turn that gets deleted becomes an empty
    assistant message, which is worse than a visible thought.
    """
    if THINK_CLOSE not in text and THINK_OPEN not in text:
        return "", text

    thinking_parts: list[str] = []

    # Case 2: a closer with no opener before it means content began inside the
    # block. Everything up to the first closer is thinking.
    first_close = text.find(THINK_CLOSE)
    first_open = text.find(THINK_OPEN)
    if first_close != -1 and (first_open == -1 or first_close < first_open):
        thinking_parts.append(text[:first_close])
        text = text[first_close + len(THINK_CLOSE):]

    # Cases 1 and 3: paired blocks, then any unterminated tail.
    while THINK_OPEN in text:
        start = text.index(THINK_OPEN)
        rest = text[start + len(THINK_OPEN):]
        if THINK_CLOSE in rest:
            end = rest.index(THINK_CLOSE)
            thinking_parts.append(rest[:end])
            text = text[:start] + rest[end + len(THINK_CLOSE):]
        else:
            thinking_parts.append(rest)
            text = text[:start]
            break

    return "\n".join(p.strip() for p in thinking_parts if p.strip()).strip(), text.strip()


def _tool_names(req: MessagesRequest) -> set[str]:
    return {t.name for t in (req.tools or [])}

# Claude Code prepends this line to the system prompt with a per-request hash
# (`cch=...`). One changing token near position 0 invalidates the ENTIRE prompt
# prefix in Ollama's KV cache, forcing a full re-prefill of a 50K-token harness
# prompt on every turn (~2 min each). Useless for local backends — strip it.
_BILLING_HEADER_RE = re.compile(r"^x-anthropic-billing-header:[^\n]*\n?")


def _system_text(system: str | list[dict[str, Any]] | None) -> str | None:
    if system is None:
        return None
    if isinstance(system, str):
        return _BILLING_HEADER_RE.sub("", system)
    # Array of content blocks — concatenate text parts
    parts = [b["text"] for b in system if b.get("type") == "text" and b.get("text")]
    return _BILLING_HEADER_RE.sub("", "\n".join(parts)) or None


def _content_to_openai(content: str | list[dict[str, Any]]) -> str | list[dict[str, Any]] | None:
    """Convert an Anthropic content value to OpenAI content."""
    if isinstance(content, str):
        return content
    # Pure text blocks → plain string; mixed → list
    text_only = all(b.get("type") == "text" for b in content)
    if text_only:
        return "".join(b.get("text", "") for b in content)
    result = []
    for block in content:
        t = block.get("type")
        if t == "text":
            result.append({"type": "text", "text": block.get("text", "")})
        elif t == "image":
            src = block.get("source", {})
            if src.get("type") == "base64":
                result.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"},
                })
            elif src.get("type") == "url":
                result.append({"type": "image_url", "image_url": {"url": src["url"]}})
    return result or None


def messages_to_openai(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert Anthropic messages list to OpenAI messages list."""
    result: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.content
        if isinstance(content, str):
            result.append({"role": msg.role, "content": content})
            continue

        # Drop thinking blocks on the way back — OpenAI-compat backends don't
        # accept them, and the reasoning already shaped the turn it belongs to.
        content = [b for b in content if b.get("type") not in ("thinking", "redacted_thinking")]

        # Detect tool_result blocks → must become role=tool messages
        has_tool_results = any(b.get("type") == "tool_result" for b in content)
        has_tool_use = any(b.get("type") == "tool_use" for b in content)

        if has_tool_results and msg.role == "user":
            # Emit each tool_result as a separate tool message, plus any text
            text_parts = [b.get("text", "") for b in content if b.get("type") == "text"]
            if text_parts:
                result.append({"role": "user", "content": "\n".join(text_parts)})
            for block in content:
                if block.get("type") == "tool_result":
                    raw = block.get("content")
                    if isinstance(raw, list):
                        text = "\n".join(b.get("text", "") for b in raw if b.get("type") == "text")
                    else:
                        text = str(raw) if raw is not None else ""
                    result.append({
                        "role": "tool",
                        "tool_call_id": block["tool_use_id"],
                        "content": text,
                    })
        elif has_tool_use and msg.role == "assistant":
            # Emit text + tool_calls
            text_parts = [b.get("text", "") for b in content if b.get("type") == "text"]
            tool_calls = []
            for block in content:
                if block.get("type") == "tool_use":
                    tool_calls.append({
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    })
            result.append({
                "role": "assistant",
                "content": "\n".join(text_parts) or None,
                "tool_calls": tool_calls,
            })
        else:
            converted = _content_to_openai(content)
            result.append({"role": msg.role, "content": converted})

    return result


def tools_to_openai(tools: list[Tool] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.input_schema,
            },
        }
        for t in tools
    ]


def _hoist_system_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Guarantee at most one system message, at index 0.

    Ollama 0.32 rejects any system message at index > 0 with HTTP 500
    "system message must be at the beginning", and that includes a payload whose
    FIRST message is a valid system message and which carries a second one later.
    0.23.4 accepted both shapes, so this only started failing when the daemon was
    upgraded on 2026-08-16 — every request in a tool-using session 500'd in about
    150ms, before any inference, which is what a validation rejection looks like
    against a load failure.

    Anthropic keeps the system prompt in its own top-level field, so a system
    message arriving inside `messages` is already unusual. Rather than drop that
    content (it is instruction text, and dropping instructions silently is worse
    than reordering them) it gets merged into the leading system block in the
    order it appeared.

    Providers differ on this: some accept system anywhere, some take only the
    first, some 500. Normalising here means the proxy sends the one shape all of
    them accept, instead of depending on which daemon is installed.
    """
    stray = [i for i, m in enumerate(messages) if m.get("role") == "system" and i > 0]
    if not stray:
        return messages

    logger.warning(
        "hoisting %d system message(s) from position(s) %s to the front — "
        "Ollama 0.32+ rejects system messages that are not first",
        len(stray),
        stray,
    )

    lead: list[str] = []
    rest: list[dict[str, Any]] = []
    for i, m in enumerate(messages):
        if m.get("role") == "system":
            content = m.get("content")
            if isinstance(content, list):
                content = "".join(
                    b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
                )
            if content:
                lead.append(str(content))
        else:
            rest.append(m)

    return ([{"role": "system", "content": "\n\n".join(lead)}] if lead else []) + rest


def _is_local_provider(settings) -> bool:
    """Is this request headed for a model running on this machine?

    Cloud sessions already receive Mem0 through the `UserPromptSubmit` hook, so
    injecting for them would duplicate the same text and spend context twice.
    Local sessions launched with `--bare` get no hooks at all, which is the gap
    this fills.
    """
    url = (getattr(settings, "provider_base_url", "") or "").lower()
    return "localhost" in url or "127.0.0.1" in url or "0.0.0.0" in url


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    """Text of the most recent user turn — the query recall is run against."""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return " ".join(
                b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"
            )
    return ""


def _inject_memory(messages: list[dict[str, Any]], settings) -> list[dict[str, Any]]:
    """Prepend durable-memory recall to the system block for local models.

    Local sessions started by the `qwen` wrapper run with `--bare`, which
    disables every hook — including the one that normally injects Mem0. Without
    this, the default local tier is the only one in the stack with no durable
    memory. Recall is read from the offline SQLite mirror so it survives the
    outage that failover exists to cover.

    Fail-open: a recall problem returns the messages untouched.
    """
    if not getattr(settings, "memory_inject", False) or not _is_local_provider(settings):
        return messages

    try:
        from . import memory as _memory

        head = messages[0] if messages else None
        if head is not None and head.get("role") == "system" and _memory.already_injected(str(head.get("content") or "")):
            return messages

        query = _last_user_text(messages)
        if not query:
            return messages

        found = _memory.recall(
            query,
            k=getattr(settings, "memory_top_k", 6),
            char_budget=getattr(settings, "memory_char_budget", 1200),
        )
        block = _memory.build_block(found)
        if not block:
            return messages

        logger.info("injected %d durable memories (%d chars)", len(found), len(block))

        out = list(messages)
        if out and out[0].get("role") == "system":
            out[0] = {**out[0], "content": f"{block}\n\n{out[0].get('content') or ''}".rstrip()}
        else:
            out.insert(0, {"role": "system", "content": block})
        return out
    except Exception as exc:  # noqa: BLE001 — never cost the user a turn
        logger.warning("memory injection skipped: %s", exc)
        return messages


def build_nim_payload(req: MessagesRequest, settings) -> dict[str, Any]:
    """Build the full NIM chat/completions payload from an Anthropic request."""
    oai_messages: list[dict[str, Any]] = []

    system_text = _system_text(req.system)
    if system_text:
        oai_messages.append({"role": "system", "content": system_text})

    oai_messages.extend(messages_to_openai(req.messages))
    oai_messages = _hoist_system_messages(oai_messages)
    oai_messages = _inject_memory(oai_messages, settings)

    payload: dict[str, Any] = {
        "model": settings.provider_model,
        "messages": oai_messages,
        "max_tokens": min(req.max_tokens, settings.provider_max_tokens),
        "stream": req.stream,
        "temperature": req.temperature if req.temperature is not None else settings.provider_temperature,
        "top_p": req.top_p if req.top_p is not None else settings.provider_top_p,
    }

    oai_tools = tools_to_openai(req.tools)
    if oai_tools:
        payload["tools"] = oai_tools
        choice = req.tool_choice
        if choice:
            if choice.type == "any":
                payload["tool_choice"] = "required"
            elif choice.type == "tool" and choice.name:
                payload["tool_choice"] = {"type": "function", "function": {"name": choice.name}}
            elif choice.type == "none":
                payload["tool_choice"] = "none"
            else:
                payload["tool_choice"] = "auto"

    if req.stop_sequences:
        payload["stop"] = req.stop_sequences

    effort = getattr(settings, "provider_reasoning_effort", "") or ""
    if effort:
        payload["reasoning_effort"] = effort

    return payload


# ---------------------------------------------------------------------------
# Response conversion (non-streaming)
# ---------------------------------------------------------------------------

def nim_response_to_anthropic(nim: dict[str, Any], req: MessagesRequest, msg_id: str) -> dict[str, Any]:
    choice = nim["choices"][0]
    finish = choice.get("finish_reason", "end_turn")
    stop_reason = _map_finish_reason(finish)

    message = choice.get("message", {})
    content: list[dict[str, Any]] = []

    structured_tool_calls = message.get("tool_calls") or []
    text = message.get("content") or ""

    # Surface reasoning (qwen3.x thinking models etc.) as an Anthropic thinking
    # block instead of dropping it — a reasoning-only turn otherwise becomes an
    # EMPTY assistant message and Claude Code shows/returns nothing.
    reasoning = message.get("reasoning") or message.get("reasoning_content") or ""

    # mlx_vlm.server leaves <think> tags inline in `content` instead of filling
    # `reasoning`. Split them out BEFORE the embedded-tool-call scan below:
    # reasoning frequently contains JSON the model is talking itself through,
    # and feeding that to extract_text_tool_calls invents tool calls the model
    # never made. See split_inline_thinking for the tag shapes.
    inline_thinking, text = split_inline_thinking(text)
    if inline_thinking:
        reasoning = f"{reasoning}\n{inline_thinking}".strip() if reasoning else inline_thinking

    if reasoning and not text and not structured_tool_calls:
        # Reasoning-only turn (qwen3.5 does this intermittently): the model put
        # its answer in reasoning and emitted no text. Promote it so the turn
        # isn't an empty assistant message.
        text = reasoning
        reasoning = ""
    if reasoning:
        content.append({"type": "thinking", "thinking": reasoning, "signature": ""})

    # Fallback: some models (qwen2.5-coder) emit the tool call as plain JSON in
    # `content` instead of structured `tool_calls`. Detect and convert it so
    # Claude Code sees real tool_use blocks.
    if not structured_tool_calls and text:
        embedded = extract_text_tool_calls(text, _tool_names(req))
        if embedded:
            for call in embedded:
                content.append({
                    "type": "tool_use",
                    "id": _new_tool_use_id(),
                    "name": call["name"],
                    "input": call["input"],
                })
            return {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": req.model,
                "content": content,
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": nim.get("usage", {}).get("prompt_tokens", 0),
                    "output_tokens": nim.get("usage", {}).get("completion_tokens", 0),
                },
            }

    if text:
        content.append({"type": "text", "text": text})

    for tc in structured_tool_calls:
        fn = tc.get("function", {})
        try:
            inp = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            inp = {}
        content.append({
            "type": "tool_use",
            "id": tc["id"],
            "name": fn["name"],
            "input": inp,
        })
        stop_reason = "tool_use"

    usage = nim.get("usage", {})
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": req.model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def _map_finish_reason(reason: str | None) -> str:
    mapping = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "content_filter": "stop_sequence",
    }
    return mapping.get(reason or "stop", "end_turn")


# ---------------------------------------------------------------------------
# Streaming conversion — OpenAI SSE chunks → Anthropic SSE events
# ---------------------------------------------------------------------------

def start_stream_events(
    state: dict,
    msg_id: str,
    req: MessagesRequest,
    input_tokens: int,
) -> list[str]:
    """Initialize streaming state and return the opening SSE events.

    Called lazily by stream_openai_to_anthropic on the first chunk, or eagerly
    by the route handler so the client gets bytes immediately (a local model
    prefilling a big prompt sends nothing for minutes otherwise)."""
    state["started"] = True
    state["block_index"] = 0
    state["tool_calls"] = {}
    state["output_tokens"] = 0
    # Content-embedded tool-call fallback (qwen2.5-coder etc.).
    # tool_mode: request offered tools, so model output *might* be a tool call.
    # text_decision: None=undecided, "text"=streaming prose, "buffer"=holding
    #   suspected tool-call JSON until finish.
    state["tool_mode"] = bool(req.tools)
    state["text_decision"] = None
    state["buf"] = ""
    state["thinking_open"] = False
    state.setdefault("strip_inline_thinking", False)
    state["inline_think_done"] = False
    state["think_carry"] = ""
    return [
        _sse("message_start", {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": req.model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": input_tokens, "output_tokens": 0},
            },
        }),
        _sse("ping", {"type": "ping"}),
    ]


def stream_openai_to_anthropic(
    chunk: dict[str, Any],
    state: dict,
    msg_id: str,
    req: MessagesRequest,
    input_tokens: int,
):
    """
    Yield Anthropic SSE event strings for a single OpenAI chunk.
    `state` is a mutable dict carried across calls:
      { started, block_index, tool_calls: {index: {id, name, args}} }
    """
    events: list[str] = []

    if not state.get("started"):
        events.extend(start_stream_events(state, msg_id, req, input_tokens))

    choices = chunk.get("choices", [])
    if not choices:
        return events

    choice = choices[0]
    delta = choice.get("delta", {})
    finish = choice.get("finish_reason")

    # Reasoning delta (qwen3.x thinking models etc.) — stream as an Anthropic
    # thinking block instead of dropping it; a reasoning-only turn otherwise
    # becomes an EMPTY assistant message and Claude Code shows/returns nothing.
    reasoning = delta.get("reasoning") or delta.get("reasoning_content")
    if reasoning:
        state["reasoning_buf"] = state.get("reasoning_buf", "") + reasoning
        if not state.get("thinking_open"):
            state["thinking_open"] = True
            events.append(_sse("content_block_start", {
                "type": "content_block_start",
                "index": state["block_index"],
                "content_block": {"type": "thinking", "thinking": ""},
            }))
        events.append(_sse("content_block_delta", {
            "type": "content_block_delta",
            "index": state["block_index"],
            "delta": {"type": "thinking_delta", "thinking": reasoning},
        }))

    # Text delta
    text = delta.get("content")

    # Backends that leave <think> tags inline (mlx_vlm.server) rather than
    # filling `reasoning`. The stream STARTS inside the block because Qwen's
    # template pre-fills the opening tag, so route deltas to a thinking block
    # until the closer arrives. See provider_strip_inline_thinking.
    if text and state.get("strip_inline_thinking") and not state.get("inline_think_done"):
        buf = state.get("think_carry", "") + text
        state["think_carry"] = ""
        if buf.lstrip().startswith(THINK_OPEN):
            buf = buf.lstrip()[len(THINK_OPEN):]
        if THINK_CLOSE in buf:
            thought, buf = buf.split(THINK_CLOSE, 1)
            state["inline_think_done"] = True
            if thought.strip():
                events.extend(_thinking_deltas(state, thought))
            _close_thinking_block(events, state)
            text = buf
        else:
            # Hold back a tail that could be a tag split across chunks, so
            # "</thi" + "nk>" is not emitted as visible text.
            keep = 0
            for n in range(1, len(THINK_CLOSE)):
                if buf.endswith(THINK_CLOSE[:n]):
                    keep = n
            if keep:
                state["think_carry"] = buf[-keep:]
                buf = buf[:-keep]
            if buf:
                events.extend(_thinking_deltas(state, buf))
            return events

    if text:
        _close_thinking_block(events, state)
        if not state["tool_mode"] or state["text_decision"] == "text":
            # Plain streaming: emit immediately.
            _emit_text_delta(events, state, text)
        else:
            # Undecided/buffering: hold text until we know it's not a tool call.
            state["buf"] += text
            if state["text_decision"] is None:
                stripped = state["buf"].lstrip()
                if stripped:  # we have a first non-space char to judge
                    if looks_like_tool_call_start(state["buf"]):
                        state["text_decision"] = "buffer"
                    else:
                        # Definitely prose/code — flush buffer and stream the rest.
                        state["text_decision"] = "text"
                        _emit_text_delta(events, state, state["buf"])
                        state["buf"] = ""

    # Tool call deltas
    for tc_delta in delta.get("tool_calls") or []:
        _close_thinking_block(events, state)
        tc_idx = tc_delta["index"]
        if tc_idx not in state["tool_calls"]:
            # Close any open text block first
            if state.get("text_block_open"):
                events.append(_sse("content_block_stop", {
                    "type": "content_block_stop",
                    "index": state["block_index"],
                }))
                state["text_block_open"] = False
                state["block_index"] += 1

            state["tool_calls"][tc_idx] = {
                "id": tc_delta.get("id", ""),
                "name": tc_delta.get("function", {}).get("name", ""),
                "block_index": state["block_index"],
            }
            state["block_index"] += 1
            events.append(_sse("content_block_start", {
                "type": "content_block_start",
                "index": state["tool_calls"][tc_idx]["block_index"],
                "content_block": {
                    "type": "tool_use",
                    "id": state["tool_calls"][tc_idx]["id"],
                    "name": state["tool_calls"][tc_idx]["name"],
                    "input": {},
                },
            }))

        args_chunk = (tc_delta.get("function") or {}).get("arguments", "")
        if args_chunk:
            events.append(_sse("content_block_delta", {
                "type": "content_block_delta",
                "index": state["tool_calls"][tc_idx]["block_index"],
                "delta": {"type": "input_json_delta", "partial_json": args_chunk},
            }))

    # Finish
    if finish:
        converted_tool_use = False
        _close_thinking_block(events, state)

        # Resolve any buffered content we were holding back.
        if state["buf"]:
            embedded = extract_text_tool_calls(state["buf"], _tool_names(req))
            if embedded:
                converted_tool_use = True
                for call in embedded:
                    idx = state["block_index"]
                    state["block_index"] += 1
                    events.append(_sse("content_block_start", {
                        "type": "content_block_start",
                        "index": idx,
                        "content_block": {
                            "type": "tool_use",
                            "id": _new_tool_use_id(),
                            "name": call["name"],
                            "input": {},
                        },
                    }))
                    events.append(_sse("content_block_delta", {
                        "type": "content_block_delta",
                        "index": idx,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(call["input"]),
                        },
                    }))
                    events.append(_sse("content_block_stop", {
                        "type": "content_block_stop", "index": idx,
                    }))
            else:
                # Not a tool call after all — emit the held text as a normal block.
                _emit_text_delta(events, state, state["buf"])
            state["buf"] = ""

        # Reasoning-only safety net: the model finished without any visible
        # text or tool call (qwen3.5 with thinking does this intermittently).
        # Promote the accumulated reasoning to a text block so the assistant
        # message isn't empty.
        if (not state.get("text_emitted") and not state["tool_calls"]
                and not converted_tool_use and state.get("reasoning_buf")):
            _emit_text_delta(events, state, state["reasoning_buf"])

        if state.get("text_block_open"):
            events.append(_sse("content_block_stop", {
                "type": "content_block_stop",
                "index": state.get("block_index", 0),
            }))
        for tc in state["tool_calls"].values():
            events.append(_sse("content_block_stop", {
                "type": "content_block_stop",
                "index": tc["block_index"],
            }))

        stop_reason = "tool_use" if converted_tool_use else _map_finish_reason(finish)
        usage = chunk.get("usage") or {}
        output_tokens = usage.get("completion_tokens", state.get("output_tokens", 0))

        events.append(_sse("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": output_tokens},
        }))
        events.append(_sse("message_stop", {"type": "message_stop"}))

    return events


def _thinking_deltas(state: dict, thought: str) -> list[str]:
    """Emit thinking deltas, opening the block on first use."""
    events: list[str] = []
    if not state.get("thinking_open"):
        state["thinking_open"] = True
        events.append(_sse("content_block_start", {
            "type": "content_block_start",
            "index": state["block_index"],
            "content_block": {"type": "thinking", "thinking": ""},
        }))
    events.append(_sse("content_block_delta", {
        "type": "content_block_delta",
        "index": state["block_index"],
        "delta": {"type": "thinking_delta", "thinking": thought},
    }))
    return events


def _close_thinking_block(events: list[str], state: dict) -> None:
    """Close an open thinking block (signature_delta keeps the Anthropic SSE
    shape valid) and advance the block index so text/tool blocks don't collide."""
    if not state.get("thinking_open"):
        return
    idx = state["block_index"]
    events.append(_sse("content_block_delta", {
        "type": "content_block_delta",
        "index": idx,
        "delta": {"type": "signature_delta", "signature": ""},
    }))
    events.append(_sse("content_block_stop", {"type": "content_block_stop", "index": idx}))
    state["thinking_open"] = False
    state["block_index"] += 1


def _emit_text_delta(events: list[str], state: dict, text: str) -> None:
    """Open a text content block if needed and append a text delta."""
    if not text:
        return
    idx = state["block_index"]
    state["text_emitted"] = True
    if not state.get("text_block_open"):
        state["text_block_open"] = True
        events.append(_sse("content_block_start", {
            "type": "content_block_start",
            "index": idx,
            "content_block": {"type": "text", "text": ""},
        }))
    events.append(_sse("content_block_delta", {
        "type": "content_block_delta",
        "index": idx,
        "delta": {"type": "text_delta", "text": text},
    }))


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
