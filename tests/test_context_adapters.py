import json
from pathlib import Path

import pytest

from src.proxy.claude_context_adapter import ClaudeContextAdapter
from src.proxy.codex_context_adapter import CodexContextAdapter
from src.proxy.models import Message, MessagesRequest


FIXTURE = Path(__file__).parent / "fixtures" / "codex_responses_request.json"


def claude_fixture() -> MessagesRequest:
    return MessagesRequest.model_validate(
        {
            "model": "claude-opus-4-6",
            "system": [{"type": "text", "text": "Keep exact request fields."}],
            "max_tokens": 1234,
            "stream": True,
            "messages": [
                {"role": "user", "content": "older task"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "Read",
                            "input": {"path": "README.md"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": "repository instructions",
                        }
                    ],
                },
                {"role": "user", "content": "active task"},
            ],
        }
    )


def load_codex_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_claude_adapter_round_trips_exact_messages():
    """Dropping no selected segment must preserve the caller's request."""
    request = claude_fixture()

    adapter = ClaudeContextAdapter()
    context = adapter.normalize(request)
    rebuilt = adapter.rebuild(context, [segment.segment_id for segment in context.segments])

    assert rebuilt.model_dump(mode="json") == request.model_dump(mode="json")


def test_claude_adapter_assigns_one_pair_id_to_tool_use_and_result():
    """A partial tool pair would make the next local request invalid."""
    context = ClaudeContextAdapter().normalize(claude_fixture())

    paired = [segment for segment in context.segments if segment.pair_id == "toolu_1"]

    assert [segment.kind for segment in paired] == ["tool_use", "tool_result"]


def test_claude_rebuild_expands_a_selected_tool_pair():
    """Selecting a call alone must also retain the response that resolves it."""
    adapter = ClaudeContextAdapter()
    context = adapter.normalize(claude_fixture())
    call = next(segment for segment in context.segments if segment.kind == "tool_use")

    rebuilt = adapter.rebuild(context, [call.segment_id])

    assert rebuilt.messages[0].content[0]["type"] == "tool_use"
    assert rebuilt.messages[1].content[0]["type"] == "tool_result"


def test_claude_adapter_rejects_a_request_without_a_textual_user_instruction():
    request = claude_fixture()
    request.messages = [
        Message(
            role="user",
            content=[
                {
                    "type": "image",
                    "source": {"type": "url", "url": "https://example.com/a.png"},
                }
            ],
        )
    ]

    with pytest.raises(ValueError, match="textual current user instruction"):
        ClaudeContextAdapter().normalize(request)


def test_claude_adapter_round_trips_an_empty_content_list():
    """An empty historical message is still exact request-body data."""
    request = claude_fixture()
    request.messages.insert(1, Message(role="assistant", content=[]))

    adapter = ClaudeContextAdapter()
    context = adapter.normalize(request)
    rebuilt = adapter.rebuild(context, [segment.segment_id for segment in context.segments])

    assert rebuilt.model_dump(mode="json") == request.model_dump(mode="json")


def test_claude_adapter_uses_text_before_a_terminal_tool_result_as_current_instruction():
    """A tool result completes the active turn instead of replacing its prompt."""
    request = claude_fixture()
    request.messages.pop()

    context = ClaudeContextAdapter().normalize(request)

    current = next(segment for segment in context.segments if segment.segment_id == context.current_segment_id)
    assert current.searchable_text == "older task"


def test_claude_rebuild_keeps_a_multipart_current_user_message_as_one_unit():
    """Selecting the current instruction must not discard its other content blocks."""
    request = MessagesRequest.model_validate(
        {
            "model": "claude-opus-4-6",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "first instruction clause"},
                        {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}},
                        {"type": "text", "text": "second instruction clause"},
                    ],
                }
            ],
        }
    )
    adapter = ClaudeContextAdapter()
    context = adapter.normalize(request)

    rebuilt = adapter.rebuild(context, [context.current_segment_id])

    assert rebuilt.model_dump(mode="json")["messages"] == request.model_dump(mode="json")["messages"]


def test_repeated_identical_segments_have_distinct_occurrence_ids():
    """Selecting one repeated turn must not retain another equal turn."""
    request = MessagesRequest.model_validate(
        {
            "model": "claude-opus-4-6",
            "messages": [
                {"role": "user", "content": "active task"},
                {"role": "assistant", "content": "same response"},
                {"role": "assistant", "content": "same response"},
            ],
        }
    )
    adapter = ClaudeContextAdapter()
    context = adapter.normalize(request)
    repeated = [segment for segment in context.segments if segment.searchable_text == "same response"]

    rebuilt = adapter.rebuild(context, [repeated[0].segment_id])

    assert repeated[0].segment_id != repeated[1].segment_id
    assert repeated[0].content_hash == repeated[1].content_hash
    assert [message.content for message in rebuilt.messages] == ["same response"]


def test_codex_adapter_round_trips_exact_input_items():
    """Dropping no selected segment must preserve all Codex request fields."""
    payload = load_codex_fixture()

    adapter = CodexContextAdapter()
    context = adapter.normalize(payload)
    rebuilt = adapter.rebuild(context, [segment.segment_id for segment in context.segments])

    assert rebuilt == payload


def test_codex_adapter_assigns_one_pair_id_to_call_and_output():
    """A function output without its call is unusable conversation state."""
    context = CodexContextAdapter().normalize(load_codex_fixture())

    paired = [segment for segment in context.segments if segment.pair_id == "call_local"]

    assert [segment.kind for segment in paired] == ["function_call", "function_call_output"]


def test_codex_rebuild_expands_a_selected_function_pair():
    """Selecting a call alone must also retain its corresponding output."""
    adapter = CodexContextAdapter()
    context = adapter.normalize(load_codex_fixture())
    call = next(segment for segment in context.segments if segment.kind == "function_call")

    rebuilt = adapter.rebuild(context, [call.segment_id])

    assert [item["type"] for item in rebuilt["input"]] == [
        "function_call",
        "function_call_output",
    ]


def test_codex_adapter_rejects_a_request_without_a_textual_user_instruction():
    payload = load_codex_fixture()
    payload["input"] = [
        {"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": "data:image/png;base64,AA"}]}
    ]

    with pytest.raises(ValueError, match="textual current user instruction"):
        CodexContextAdapter().normalize(payload)
