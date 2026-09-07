"""Local models receive enough context to recover from a rejected tool call."""

from src.proxy.models import Message
from src.proxy.translate import messages_to_openai


def test_schema_validation_error_tells_the_model_to_change_its_arguments() -> None:
    messages = [
        Message(
            role="assistant",
            content=[
                {
                    "type": "tool_use",
                    "id": "call-1",
                    "name": "Bash",
                    "input": {"query": "Firecrawl features"},
                }
            ],
        ),
        Message(
            role="user",
            content=[
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": (
                        "<tool_use_error>InputValidationError: Bash failed because "
                        "the required field `command` is missing</tool_use_error>"
                    ),
                }
            ],
        ),
    ]

    translated = messages_to_openai(messages)

    assert translated[-1]["role"] == "tool"
    assert "Use the tool's declared field names" in translated[-1]["content"]
    assert "Do not repeat the rejected arguments" in translated[-1]["content"]


def test_ordinary_tool_result_is_not_modified() -> None:
    messages = [
        Message(
            role="user",
            content=[
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": "command output",
                }
            ],
        )
    ]

    translated = messages_to_openai(messages)

    assert translated == [
        {"role": "tool", "tool_call_id": "call-1", "content": "command output"}
    ]
