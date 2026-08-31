"""Bounded, lineage-local retrieval for Backdoor's internal search round."""

import json

from src.proxy.context_runtime import (
    INTERNAL_SEARCH_TOOL_NAME,
    InternalSearchRequest,
    build_internal_search_followup,
    parse_internal_search,
)
from src.proxy.context_store import ContextStore
from src.proxy.models import Message, MessagesRequest


def complete_call(query: str) -> dict:
    return {
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "content": None,
                "tool_calls": [{
                    "id": "ctx-1",
                    "type": "function",
                    "function": {
                        "name": INTERNAL_SEARCH_TOOL_NAME,
                        "arguments": json.dumps({"query": query}),
                    },
                }],
            },
        }],
    }


def test_parse_internal_search_accepts_complete_and_streamed_calls():
    complete = parse_internal_search([complete_call("rollback revision")])
    streamed = parse_internal_search([
        {
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "ctx-2",
                        "function": {
                            "name": "backdoor_context_",
                            "arguments": '{"query":"rollback ',
                        },
                    }],
                },
                "finish_reason": None,
            }],
        },
        {
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "function": {
                            "name": "search",
                            "arguments": 'revision"}',
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        },
    ])

    assert complete == InternalSearchRequest(query="rollback revision", tool_call_id="ctx-1")
    assert streamed == InternalSearchRequest(query="rollback revision", tool_call_id="ctx-2")


def test_build_followup_stays_in_lineage_and_caps_six_segments(tmp_path):
    store = ContextStore(tmp_path / "transcripts.sqlite3")
    messages = []
    for index in range(10):
        messages.extend([
            Message(role="user", content=f"rollback revision segment {index} needle"),
            Message(role="assistant", content=f"recorded {index}"),
        ])
    archived = MessagesRequest(model="claude-opus-5", messages=messages)
    lineage = store.archive_request(archived)
    other = store.archive_request(MessagesRequest(
        model="claude-opus-5",
        messages=[Message(role="user", content="other-root rollback revision right-secret")],
    ))
    assert other.lineage_id != lineage.lineage_id

    selected = MessagesRequest(
        model="claude-opus-5",
        messages=[Message(role="user", content="find the rollback revision")],
    )
    followup = build_internal_search_followup(
        selected,
        InternalSearchRequest(query="rollback revision", tool_call_id="ctx-3"),
        lineage.lineage_id,
        store,
        lambda text: len(text.split()),
        result_tokens=80,
    )

    assistant = followup.messages[-2].content
    result = followup.messages[-1].content
    assert isinstance(assistant, list) and assistant[0]["name"] == INTERNAL_SEARCH_TOOL_NAME
    assert isinstance(result, list) and result[0]["tool_use_id"] == "ctx-3"
    text = result[0]["content"]
    assert text.count("<segment ") <= 6
    assert len(text.split()) <= 80
    assert "right-secret" not in text
    assert "untrusted transcript data" in text
