import gzip
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.proxy.codex_context import (
    CodexRequestError,
    build_local_payload,
    decode_codex_body,
    extract_recall_query,
)
from src.proxy.config import Settings


FIXTURE = Path(__file__).parent / "fixtures" / "codex_responses_request.json"


def load_request():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def content_text(payload):
    texts = []
    for item in payload["input"]:
        for content in item.get("content", []):
            if isinstance(content, dict) and isinstance(content.get("text"), str):
                texts.append(content["text"])
    return "\n".join(texts)


def test_decode_codex_body_accepts_identity_and_gzip_without_mutation():
    raw = FIXTURE.read_bytes()

    assert decode_codex_body(raw, "") == load_request()
    assert decode_codex_body(gzip.compress(raw), "gzip") == load_request()


@pytest.mark.parametrize(
    ("body", "encoding", "status"),
    [
        (b"{}", "br", 415),
        (b"not-json", "", 400),
        (b"[]", "", 400),
    ],
)
def test_decode_codex_body_rejects_unsupported_or_malformed_requests(
    body, encoding, status
):
    with pytest.raises(CodexRequestError) as caught:
        decode_codex_body(body, encoding)

    assert caught.value.status_code == status


def test_build_local_payload_starts_at_active_user_and_injects_cognee_context():
    cloud = load_request()
    local, budget = build_local_payload(
        cloud,
        ["decision one", "decision two"],
        Settings(),
    )
    rendered = json.dumps(local)
    text = content_text(local)

    assert local["model"] == "qwen3.8:27b-obliterated"
    assert local["stream"] is True
    assert "Relevant context recalled from local Cognee" in text
    assert "decision one" in text
    assert "active task" in text
    assert "bounded result" in rendered
    assert "older task" not in rendered
    assert "older answer" not in rendered
    assert "cloud-only instructions" not in rendered
    assert "cloud-only" not in rendered
    assert "remove-me" not in rendered
    assert budget.input_tokens <= 28_000


def test_build_local_payload_flattens_local_namespace_and_drops_remote_tools():
    local, budget = build_local_payload(load_request(), [], Settings())

    assert local["tools"] == [
        {
            "type": "function",
            "name": "exec",
            "description": "Run a local tool",
            "strict": False,
            "parameters": {
                "type": "object",
                "properties": {"source": {"type": "string"}},
                "required": ["source"],
                "additionalProperties": False,
            },
        }
    ]
    assert budget.dropped_tools == 2


def test_empty_local_tool_allowlist_removes_tools_and_tool_choice():
    local, budget = build_local_payload(
        load_request(), [], Settings(codex_local_tools="")
    )

    assert "tools" not in local
    assert "tool_choice" not in local
    assert budget.dropped_tools == 3


def test_extract_recall_query_uses_only_the_latest_user_text():
    assert extract_recall_query(load_request()) == "active task"


def test_build_local_payload_never_truncates_latest_text_instruction():
    cloud = load_request()
    latest = "keep-this-instruction " * 100
    cloud["input"][4]["content"][0]["text"] = latest
    settings = Settings(
        codex_context_window=100,
        codex_reply_reserve_tokens=20,
        codex_system_budget_tokens=10,
        codex_memory_budget_tokens=10,
        codex_tools_budget_tokens=10,
        codex_active_turn_budget_tokens=50,
    )

    with pytest.raises(CodexRequestError) as caught:
        build_local_payload(cloud, ["discardable memory"], settings)

    assert caught.value.status_code == 413


def test_codex_component_budgets_must_fit_context_window():
    with pytest.raises(ValidationError):
        Settings(
            codex_context_window=100,
            codex_reply_reserve_tokens=20,
            codex_system_budget_tokens=20,
            codex_memory_budget_tokens=20,
            codex_tools_budget_tokens=20,
            codex_active_turn_budget_tokens=21,
        )
