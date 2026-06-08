"""Translate between Anthropic Messages API format and OpenAI/NIM format."""

import json
import re
import uuid
from typing import Any

from .models import MessagesRequest, Message, Tool


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


def _tool_names(req: MessagesRequest) -> set[str]:
    return {t.name for t in (req.tools or [])}

def _system_text(system: str | list[dict[str, Any]] | None) -> str | None:
    if system is None:
        return None
    if isinstance(system, str):
        return system
    # Array of content blocks — concatenate text parts
    parts = [b["text"] for b in system if b.get("type") == "text" and b.get("text")]
    return "\n".join(parts) or None


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


def build_nim_payload(req: MessagesRequest, settings) -> dict[str, Any]:
    """Build the full NIM chat/completions payload from an Anthropic request."""
    oai_messages: list[dict[str, Any]] = []

    system_text = _system_text(req.system)
    if system_text:
        oai_messages.append({"role": "system", "content": system_text})

    oai_messages.extend(messages_to_openai(req.messages))

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
        events.append(_sse("message_start", {
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
        }))
        events.append(_sse("ping", {"type": "ping"}))

    choices = chunk.get("choices", [])
    if not choices:
        return events

    choice = choices[0]
    delta = choice.get("delta", {})
    finish = choice.get("finish_reason")

    # Text delta
    text = delta.get("content")
    if text:
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


def _emit_text_delta(events: list[str], state: dict, text: str) -> None:
    """Open a text content block if needed and append a text delta."""
    if not text:
        return
    idx = state["block_index"]
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
