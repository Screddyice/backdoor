"""Bare mode: stripping the Claude Code harness off a failed-over request."""

import pytest

from src.proxy.bare import (
    DEFAULT_KEEP_TOOLS,
    DEFAULT_SYSTEM,
    make_bare,
    parse_keep,
)
from src.proxy.models import MessagesRequest, Message, Tool
from src.proxy.tokens import count_messages


def req(**kw) -> MessagesRequest:
    base = dict(model="claude-opus-5", messages=[Message(role="user", content="hi")])
    base.update(kw)
    return MessagesRequest(**base)


# ── keep-list parsing ────────────────────────────────────────────────────────

def test_parse_keep_splits_and_trims():
    assert parse_keep("mem0, jarvis ") == ("mem0", "jarvis")


def test_parse_keep_empty_string_keeps_nothing():
    assert parse_keep("") == ()
    assert parse_keep(None) == DEFAULT_KEEP_TOOLS


def test_default_keeps_no_tools():
    """Regression lock on a failure found only by testing against a real model:
    deepseek-r1:14b makes Ollama reject any request carrying tool definitions
    ("does not support tools", HTTP 400), so a non-empty default keep-list turns
    every failover into a dead session. Mem0 — the tool that would otherwise be
    worth keeping — is cloud-backed and therefore unreachable during the only
    condition that opens the breaker. Local Mem0 recall arrives as prompt text
    instead, which needs no tool support.

    Do not reintroduce a default here without confirming the active failover
    tier accepts tools AND that the tool functions offline."""
    assert DEFAULT_KEEP_TOOLS == ()


# ── system prompt ────────────────────────────────────────────────────────────

def test_harness_system_prompt_is_replaced():
    huge = "You are Claude Code. " * 20_000
    out = make_bare(req(system=huge))
    assert out.system == DEFAULT_SYSTEM
    assert len(out.system) < len(huge) / 100


def test_system_can_be_dropped_entirely():
    assert make_bare(req(system="x"), system=None).system is None


# ── tool definitions ─────────────────────────────────────────────────────────

def test_only_kept_tools_survive():
    """The keep-list mechanism itself, exercised with an explicit list — the
    shipped default keeps nothing (see test_default_keeps_no_tools)."""
    out = make_bare(req(tools=[
        Tool(name="Bash", description="run", input_schema={"a": 1}),
        Tool(name="mcp__plugin_mem0_mem0__search_memories", description="recall"),
        Tool(name="Read"),
    ]), keep=("mem0",))
    assert [t.name for t in out.tools] == ["mcp__plugin_mem0_mem0__search_memories"]


def test_default_strips_every_tool():
    out = make_bare(req(tools=[
        Tool(name="Bash"),
        Tool(name="mcp__plugin_mem0_mem0__search_memories"),
    ]))
    assert out.tools is None


def test_no_surviving_tools_clears_tools_and_tool_choice():
    """tools=[] with a tool_choice set is a 400 on most OpenAI-compatible
    backends, so both have to go together."""
    from src.proxy.models import ToolChoice
    out = make_bare(req(tools=[Tool(name="Bash")], tool_choice=ToolChoice(type="auto")))
    assert out.tools is None
    assert out.tool_choice is None


def test_keep_match_is_case_insensitive_substring():
    out = make_bare(req(tools=[Tool(name="MCP__Plugin_MEM0__add")]), keep=("mem0",))
    assert out.tools is not None and len(out.tools) == 1


# ── transcript ───────────────────────────────────────────────────────────────

def test_conversation_text_is_preserved():
    """Failover exists to preserve the session; the messages ARE the session."""
    out = make_bare(req(messages=[
        Message(role="user", content="what did we decide about the router?"),
        Message(role="assistant", content="we made it fail over"),
    ]))
    assert out.messages[0].content == "what did we decide about the router?"
    assert out.messages[1].content == "we made it fail over"


def test_stripped_tool_result_is_truncated_to_budget():
    out = make_bare(
        req(messages=[
            Message(role="assistant", content=[
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
            ]),
            Message(role="user", content=[
                {"type": "tool_result", "tool_use_id": "t1", "content": "X" * 50_000},
            ]),
        ]),
        tool_result_chars=100,
    )
    body = out.messages[1].content
    assert isinstance(body, str)
    assert "omitted offline" in body
    assert len(body) < 400


def test_kept_tool_result_is_not_truncated():
    """A kept tool's output must survive intact — truncating it would defeat
    keeping the tool at all."""
    payload = "recalled fact " * 1000
    out = make_bare(
        req(messages=[
            Message(role="assistant", content=[
                {"type": "tool_use", "id": "m1",
                 "name": "mcp__plugin_mem0_mem0__search_memories", "input": {}},
            ]),
            Message(role="user", content=[
                {"type": "tool_result", "tool_use_id": "m1", "content": payload},
            ]),
        ]),
        keep=("mem0",),
        tool_result_chars=100,
    )
    blocks = out.messages[1].content
    assert isinstance(blocks, list)
    assert blocks[0]["content"] == payload


def test_images_are_replaced_not_forwarded():
    """Base64 image data is huge and these models are text-only."""
    out = make_bare(req(messages=[
        Message(role="user", content=[
            {"type": "text", "text": "look at this"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": "A" * 200_000}},
        ]),
    ]))
    content = out.messages[0].content
    assert "A" * 1000 not in str(content)
    assert "image omitted" in str(content)


def test_orphan_tool_result_without_matching_tool_use_is_still_reduced():
    """Claude Code truncates history, so a tool_result can arrive with its
    tool_use long gone. Falling through unreduced would reintroduce exactly the
    payload bare mode exists to remove."""
    out = make_bare(
        req(messages=[
            Message(role="user", content=[
                {"type": "tool_result", "tool_use_id": "vanished", "content": "Y" * 50_000},
            ]),
        ]),
        tool_result_chars=100,
    )
    assert len(str(out.messages[0].content)) < 400


def test_string_content_messages_pass_through_untouched():
    out = make_bare(req(messages=[Message(role="user", content="plain")]))
    assert out.messages[0].content == "plain"


# ── the property that motivates the whole module ─────────────────────────────

def test_bare_mode_collapses_a_realistic_harness_request():
    """The measured failure: a session whose system prompt and tool definitions
    alone exceeded the context window before any conversation."""
    heavy = req(
        system="CLAUDE.md and harness instructions. " * 5_000,
        tools=[Tool(name=f"tool_{i}", description="d" * 400,
                    input_schema={"properties": {"p": {"type": "string"}}})
               for i in range(60)],
        messages=[
            Message(role="user", content="why did the deploy fail?"),
            Message(role="assistant", content=[
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"cmd": "logs"}},
            ]),
            Message(role="user", content=[
                {"type": "tool_result", "tool_use_id": "t1", "content": "L" * 100_000},
            ]),
        ],
    )
    before = count_messages(heavy.messages, heavy.system, heavy.tools)
    out = make_bare(heavy)
    after = count_messages(out.messages, out.system, out.tools)

    assert after < before / 10, f"{before} → {after}"
    # and the actual question survives
    assert "why did the deploy fail?" in str(out.messages[0].content)


def test_make_bare_does_not_mutate_the_original():
    """The unstripped request is still relayed upstream once the network is
    back, so stripping must not reach into it."""
    original = req(system="keep me", tools=[Tool(name="Bash")])
    make_bare(original)
    assert original.system == "keep me"
    assert original.tools is not None and original.tools[0].name == "Bash"
