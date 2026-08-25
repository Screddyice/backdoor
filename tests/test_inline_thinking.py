"""Inline <think> tags must never reach the user as visible content.

Ollama reports reasoning in a separate `reasoning` field and honors
reasoning_effort. mlx_vlm.server does neither: it leaves the tags inline in
`content`, and Qwen's chat template pre-fills the opening `<think>` into the
assistant turn, so a stream begins INSIDE the block. Before this, a `qwen`
session rendered the model's reasoning, then a bare `</think>`, then the answer.

Thinking is re-homed into an Anthropic thinking block rather than deleted: a
reasoning-only turn that gets deleted becomes an empty assistant message.
"""

import json

from src.proxy.models import MessagesRequest
from src.proxy.translate import (
    nim_response_to_anthropic,
    split_inline_thinking,
    stream_openai_to_anthropic,
)


def _req() -> MessagesRequest:
    return MessagesRequest.model_validate(
        {"model": "qwen", "max_tokens": 100,
         "messages": [{"role": "user", "content": "hi"}]}
    )


# --- the splitter ---------------------------------------------------------

def test_paired_tags():
    assert split_inline_thinking("<think>why</think>answer") == ("why", "answer")


def test_closer_only_is_the_common_case():
    """Qwen pre-fills the opener, so content starts inside the block."""
    assert split_inline_thinking("why</think>answer") == ("why", "answer")


def test_unterminated_block_is_all_thinking():
    assert split_inline_thinking("<think>cut off") == ("cut off", "")


def test_untagged_text_is_untouched():
    assert split_inline_thinking("just an answer") == ("", "just an answer")


def test_multiple_blocks_are_collected():
    thinking, text = split_inline_thinking("a</think>b<think>c</think>d")
    assert thinking == "a\nc"
    assert text == "bd"


# --- non-streaming --------------------------------------------------------

def _nim(content: str) -> dict:
    return {"choices": [{"finish_reason": "stop",
                         "message": {"content": content}}]}


def test_non_streaming_moves_thinking_out_of_text():
    out = nim_response_to_anthropic(
        _nim("why</think>answer"), _req(), "msg_1", strip_inline_thinking=True
    )
    kinds = [b["type"] for b in out["content"]]
    assert "thinking" in kinds
    text = "".join(b["text"] for b in out["content"] if b["type"] == "text")
    assert text == "answer"
    assert "</think>" not in text


def test_non_streaming_reasoning_only_turn_is_not_empty():
    """Deleting the block would leave an assistant message with no content."""
    out = nim_response_to_anthropic(
        _nim("<think>only this</think>"), _req(), "msg_1", strip_inline_thinking=True
    )
    assert out["content"], "turn must not be empty"


def test_non_streaming_off_by_default_keeps_literal_tags():
    """The gate exists so ordinary prose survives.

    "to close a thinking block you write </think>" is a reasonable thing for a
    coding assistant to say. Ungated, everything before the tag was silently
    reclassified as reasoning and the visible answer became "at the end."
    Backends that emit real inline tags opt in per profile.
    """
    prose = "To close a thinking block you write </think> at the end."
    out = nim_response_to_anthropic(_nim(prose), _req(), "msg_1")
    text = "".join(b["text"] for b in out["content"] if b["type"] == "text")
    assert text == prose
    assert not [b for b in out["content"] if b["type"] == "thinking"]


# --- streaming ------------------------------------------------------------

def _chunk(text: str) -> dict:
    return {"choices": [{"delta": {"content": text}, "finish_reason": None}]}


def _texts(events: list[str]) -> str:
    out = ""
    for e in events:
        for line in e.splitlines():
            if not line.startswith("data: "):
                continue
            d = json.loads(line[6:])
            if d.get("type") == "content_block_delta" and d["delta"].get("type") == "text_delta":
                out += d["delta"]["text"]
    return out


def test_streaming_strips_when_enabled():
    state: dict = {"strip_inline_thinking": True}
    events: list[str] = []
    for part in ["reason", "ing", "</think>", "the answer"]:
        events += stream_openai_to_anthropic(_chunk(part), state, "msg_1", _req(), 5)
    assert _texts(events) == "the answer"


def test_streaming_handles_a_tag_split_across_chunks():
    """The carry buffer exists so "</thi" + "nk>" is not shown as text."""
    state: dict = {"strip_inline_thinking": True}
    events: list[str] = []
    for part in ["reasoning", "</thi", "nk>", "answer"]:
        events += stream_openai_to_anthropic(_chunk(part), state, "msg_1", _req(), 5)
    visible = _texts(events)
    assert visible == "answer"
    assert "think" not in visible


def test_streaming_leaves_other_tiers_alone():
    """Ollama tiers fill `reasoning`; they must not pay for this."""
    state: dict = {"strip_inline_thinking": False}
    events: list[str] = []
    for part in ["plain ", "answer"]:
        events += stream_openai_to_anthropic(_chunk(part), state, "msg_1", _req(), 5)
    assert _texts(events) == "plain answer"
