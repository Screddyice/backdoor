"""Bare mode: stripping the Claude Code harness off a failed-over request."""

import pytest

from src.proxy.bare import (
    DEFAULT_KEEP_TOOLS,
    DEFAULT_SYSTEM,
    OFFLINE_SYSTEM,
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


def test_default_keeps_local_tools_only():
    """The default keeps harness-local tools and drops every MCP tool.

    Local tools (Read, Edit, Bash, Glob, Grep) touch nothing but this disk, so
    they work while the host is offline — which is the only condition that opens
    the breaker. MCP tools are remote integrations and are dead for exactly as
    long as failover is active, and they are also where the token weight lives.

    Pairing constraint: this default REQUIRES a tool-capable tier. deepseek-r1
    makes Ollama reject any request carrying tool definitions with HTTP 400,
    which kills the session. If the tier ever moves back to a model without tool
    support, `failover_keep_tools` must go back to ""."""
    assert DEFAULT_KEEP_TOOLS == ("local",)


def test_local_token_keeps_harness_tools_and_drops_mcp():
    out = make_bare(req(tools=[
        Tool(name="Read"), Tool(name="Bash"), Tool(name="Grep"),
        Tool(name="mcp__plugin_mem0_mem0__search_memories"),
        Tool(name="mcp__apify__search-actors"),
        Tool(name="mcp__example__crm_list"),
    ]))
    assert [t.name for t in out.tools] == ["Read", "Bash", "Grep"]


def test_local_token_is_a_prefix_rule_not_a_substring():
    """An MCP tool whose name merely contains "local" is still remote."""
    out = make_bare(req(tools=[
        Tool(name="mcp__local_files__read"),
        Tool(name="Read"),
    ]))
    assert [t.name for t in out.tools] == ["Read"]


def test_explicit_entries_compose_with_the_local_token():
    """Add a specific MCP tool back without losing the local ones."""
    out = make_bare(req(tools=[
        Tool(name="Read"),
        Tool(name="mcp__plugin_mem0_mem0__search_memories"),
        Tool(name="mcp__apify__search-actors"),
    ]), keep=("local", "mem0"))
    assert [t.name for t in out.tools] == [
        "Read", "mcp__plugin_mem0_mem0__search_memories",
    ]


def test_empty_keep_list_still_drops_everything():
    """The escape hatch for a tier that cannot accept tools at all."""
    out = make_bare(req(tools=[Tool(name="Read"), Tool(name="Bash")]), keep=())
    assert out.tools is None


# ── system prompt ────────────────────────────────────────────────────────────

def test_harness_system_prompt_is_replaced():
    huge = "You are Claude Code. " * 20_000
    out = make_bare(req(system=huge))
    assert out.system == DEFAULT_SYSTEM
    assert len(out.system) < len(huge) / 100


def test_default_system_exposes_optional_internet_tools():
    out = make_bare(req(system="huge harness"))
    assert "WebSearch" in out.system
    assert "WebFetch" in out.system
    assert "curl" in out.system
    assert "lost its network connection" not in out.system


def test_offline_system_does_not_invite_network_calls():
    out = make_bare(req(system="huge harness"), system=OFFLINE_SYSTEM)
    assert "lost its network connection" in out.system
    assert "WebSearch" not in out.system


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


def test_default_strips_every_mcp_tool():
    out = make_bare(req(tools=[
        Tool(name="mcp__plugin_mem0_mem0__search_memories"),
        Tool(name="mcp__apify__search-actors"),
    ]))
    assert out.tools is None


def test_no_surviving_tools_clears_tools_and_tool_choice():
    """tools=[] with a tool_choice set is a 400 on most OpenAI-compatible
    backends, so both have to go together."""
    from src.proxy.models import ToolChoice
    out = make_bare(
        req(tools=[Tool(name="mcp__apify__x")], tool_choice=ToolChoice(type="auto"))
    )
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


def test_dropped_tool_result_is_flattened_and_truncated():
    """A dropped tool's result collapses to plain text: with the definition
    gone, a structured block referencing a tool the model can no longer call is
    just overhead."""
    out = make_bare(
        req(messages=[
            Message(role="assistant", content=[
                {"type": "tool_use", "id": "t1", "name": "mcp__apify__crawl", "input": {}},
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


def test_external_context_capsule_gets_its_own_bounded_budget():
    """The general 2K tool-result cap must not erase the ranked link capsule.

    The capsule has already discarded the full page and is independently
    bounded. Bare mode may retain up to 6K characters from this one marked
    result without reopening the unbounded-tool-result bug.
    """
    capsule = "<qwen-external-context source=example>\n" + ("relevant passage " * 300)
    out = make_bare(
        req(messages=[Message(role="user", content=[{
            "type": "tool_result", "tool_use_id": "fetch", "content": capsule,
        }])]),
        tool_result_chars=100,
    )
    body = str(out.messages[0].content)
    assert len(body) > 100
    assert len(body) < 6_500


def test_kept_tool_result_keeps_its_structure_but_is_still_truncated():
    """The keep-list decides what the model may CALL, not how much history it
    drags along. A kept tool's block stays structured so a mid-loop tool_use_id
    still lines up, but its payload is trimmed like any other: one past Read of
    a large file can outweigh the entire conversation."""
    payload = "recalled fact " * 1000
    out = make_bare(
        req(messages=[
            Message(role="assistant", content=[
                {"type": "tool_use", "id": "m1", "name": "Read", "input": {}},
            ]),
            Message(role="user", content=[
                {"type": "tool_result", "tool_use_id": "m1", "content": payload},
            ]),
        ]),
        tool_result_chars=100,
    )
    blocks = out.messages[1].content
    assert isinstance(blocks, list)
    assert blocks[0]["type"] == "tool_result"
    assert blocks[0]["tool_use_id"] == "m1"          # structure preserved
    assert len(blocks[0]["content"]) < 400           # payload still bounded
    assert "omitted offline" in blocks[0]["content"]


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
        # MCP servers are where the measured ~286K tokens actually came from.
        tools=[Tool(name=f"mcp__server{i}__do_thing", description="d" * 400,
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
