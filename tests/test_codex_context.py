import gzip
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.proxy.codex_context import (
    CodexRequestError,
    _trim_active_items,
    build_local_payload,
    decode_codex_body,
    extract_recall_query,
)
from src.proxy.config import Settings
from src.proxy.tokens import count_text


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


def test_decode_codex_body_bounds_gzip_output_before_json_parsing():
    compressed = gzip.compress(json.dumps({"padding": "A" * 1_000}).encode())

    with pytest.raises(CodexRequestError) as caught:
        decode_codex_body(compressed, "gzip", max_decoded_bytes=100)

    assert caught.value.status_code == 413


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


def test_build_local_payload_starts_at_active_user_and_injects_memory_context():
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
    assert "Relevant context recalled from local memory" in text
    assert "decision one" in text
    assert "active task" in text
    assert "bounded result" in rendered
    assert "older task" not in rendered
    assert "older answer" not in rendered
    assert "cloud-only instructions" not in rendered
    assert "cloud-only" not in rendered
    assert "remove-me" not in rendered
    assert budget.input_tokens <= 28_000
    assert local["input"][0]["role"] == "developer"
    assert "decision one" not in json.dumps(local["input"][0])
    assert local["input"][1]["role"] == "user"
    assert "Relevant context recalled from local memory" in json.dumps(
        local["input"][1]
    )


def test_build_local_payload_disables_unverifiable_ollama_reasoning_items():
    local, _ = build_local_payload(load_request(), [], Settings())

    assert local["reasoning"] == {"effort": "none"}


def test_build_local_payload_drops_cloud_only_items_after_active_user():
    cloud = load_request()
    cloud["input"].extend(
        [
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "late cloud instruction"}],
            },
            {
                "type": "additional_tools",
                "role": "developer",
                "tools": [{"type": "web_search", "external_web_access": True}],
            },
        ]
    )

    local, _ = build_local_payload(cloud, [], Settings())
    rendered = json.dumps(local)

    assert "bounded result" in rendered
    assert "late cloud instruction" not in rendered
    assert "additional_tools" not in rendered
    assert "web_search" not in rendered


def test_build_local_payload_drops_trailing_additional_local_tools():
    cloud = load_request()
    cloud["input"].append(
        {
            "type": "additional_tools",
            "role": "developer",
            "tools": [
                {
                    "type": "function",
                    "name": "evil_local_action",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        }
    )

    local, _ = build_local_payload(cloud, [], Settings())

    assert [tool["name"] for tool in local["tools"]] == ["exec"]


def test_build_local_payload_drops_an_unpaired_function_output():
    cloud = load_request()
    cloud["input"].append(
        {
            "type": "function_call_output",
            "call_id": "call_orphan",
            "output": "orphan-marker",
        }
    )

    local, _ = build_local_payload(cloud, [], Settings())

    assert "orphan-marker" not in json.dumps(local)


def test_build_local_payload_drops_a_reused_call_id_output():
    cloud = load_request()
    cloud["input"].append(
        {
            "type": "function_call_output",
            "call_id": "call_local",
            "output": "reused-id-poison",
        }
    )

    local, _ = build_local_payload(cloud, [], Settings())
    rendered = json.dumps(local)

    assert "bounded result" in rendered
    assert "reused-id-poison" not in rendered


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


def test_default_local_allowlist_drops_flat_and_nested_mcp_function_names():
    cloud = load_request()
    cloud["tools"] = [
        {
            "type": "function",
            "name": "mcp__remote__steal",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "type": "namespace",
            "name": "functions",
            "tools": [
                {
                    "type": "function",
                    "name": "mcp__remote__nested",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        },
    ]

    local, _ = build_local_payload(cloud, [], Settings())

    assert [tool["name"] for tool in local["tools"]] == ["exec"]


def test_empty_local_tool_allowlist_removes_tools_and_tool_choice():
    local, budget = build_local_payload(
        load_request(), [], Settings(codex_local_tools="")
    )

    assert "tools" not in local
    assert "tool_choice" not in local
    assert budget.dropped_tools == 3


def test_removed_explicit_tool_choice_fails_local_routing():
    cloud = load_request()
    cloud["tool_choice"] = {"type": "function", "name": "search"}

    with pytest.raises(CodexRequestError) as caught:
        build_local_payload(cloud, [], Settings())

    assert caught.value.status_code == 413


@pytest.mark.parametrize(
    "choice",
    [
        "required",
        {
            "type": "allowed_tools",
            "mode": "required",
            "tools": [{"type": "function", "name": "search"}],
        },
    ],
)
def test_required_choice_fails_when_no_local_tool_survives(choice):
    cloud = load_request()
    cloud["tool_choice"] = choice

    with pytest.raises(CodexRequestError) as caught:
        build_local_payload(cloud, [], Settings(codex_local_tools=""))

    assert caught.value.status_code == 413


@pytest.mark.parametrize(
    "choice",
    [
        "exec",
        {"type": "web_search", "name": "exec", "unexpected": "marker"},
    ],
)
def test_malformed_explicit_tool_choice_falls_back_to_auto(choice):
    cloud = load_request()
    cloud["tool_choice"] = choice

    local, _ = build_local_payload(cloud, [], Settings())

    assert local["tool_choice"] == "auto"


def test_allowed_tools_choice_intersects_the_local_tool_set():
    cloud = load_request()
    cloud["tool_choice"] = {
        "type": "allowed_tools",
        "mode": "required",
        "tools": [{"type": "function", "name": "exec"}],
    }

    local, _ = build_local_payload(cloud, [], Settings())

    assert [tool["name"] for tool in local["tools"]] == ["exec"]
    assert local["tool_choice"] == "required"


def test_allowed_tools_choice_with_no_local_match_removes_all_tools():
    cloud = load_request()
    cloud["tool_choice"] = {
        "type": "allowed_tools",
        "mode": "auto",
        "tools": [{"type": "function", "name": "search"}],
    }

    local, _ = build_local_payload(cloud, [], Settings())

    assert "tools" not in local
    assert "tool_choice" not in local


@pytest.mark.parametrize(
    "choice",
    [
        {"type": "function", "name": "target"},
        {
            "type": "allowed_tools",
            "mode": "required",
            "tools": [{"type": "function", "name": "target"}],
        },
    ],
)
def test_selected_tool_gets_budget_before_unrelated_tools(choice):
    cloud = load_request()
    first = {
        "type": "function",
        "name": "first",
        "description": "optional tool",
        "parameters": {"type": "object", "properties": {}},
    }
    target = {
        "type": "function",
        "name": "target",
        "description": "selected tool",
        "parameters": {"type": "object", "properties": {}},
    }
    cloud["input"][0]["tools"] = [first, target]
    cloud["tool_choice"] = choice
    settings = Settings(
        codex_tools_budget_tokens=count_text(json.dumps(target)) + 2,
    )

    local, _ = build_local_payload(cloud, [], settings)

    assert [tool["name"] for tool in local["tools"]] == ["target"]
    assert local["tool_choice"] in (
        "required",
        {"type": "function", "name": "target"},
    )


@pytest.mark.parametrize(
    "choice",
    [
        {"type": "function", "name": "target"},
        {
            "type": "allowed_tools",
            "mode": "required",
            "tools": [{"type": "function", "name": "target"}],
        },
    ],
)
def test_required_selected_tool_fails_if_its_schema_exceeds_local_budget(choice):
    cloud = load_request()
    cloud["input"][0]["tools"] = [
        {
            "type": "function",
            "name": "target",
            "description": "large selected tool",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string", "description": "x" * 500}},
            },
        }
    ]
    cloud["tool_choice"] = choice

    with pytest.raises(CodexRequestError) as caught:
        build_local_payload(
            cloud,
            [],
            Settings(codex_tools_budget_tokens=10),
        )

    assert caught.value.status_code == 413


def test_extract_recall_query_uses_only_the_latest_user_text():
    assert extract_recall_query(load_request()) == "active task"


def test_build_local_payload_accepts_a_paired_tool_continuation_without_user_text():
    cloud = load_request()
    cloud["input"] = cloud["input"][-2:]

    local, _ = build_local_payload(cloud, [], Settings())
    rendered = json.dumps(local)

    assert "call_local" in rendered
    assert "bounded result" in rendered
    assert extract_recall_query(cloud) == "Continue after local tool calls: exec"
    assert "pwd" not in extract_recall_query(cloud)


def test_trimmed_tool_continuation_keeps_a_synthetic_instruction():
    cloud = load_request()
    cloud["input"] = cloud["input"][-2:]
    cloud["input"][0]["arguments"] = json.dumps({"source": "x" * 8_000})
    cloud["input"][1]["output"] = "y" * 20_000
    settings = Settings(
        codex_context_window=1_000,
        codex_reply_reserve_tokens=300,
        codex_system_budget_tokens=100,
        codex_memory_budget_tokens=100,
        codex_tools_budget_tokens=100,
        codex_active_turn_budget_tokens=100,
    )

    local, budget = build_local_payload(cloud, [], settings)

    assert "Continue after local tool calls: exec" in content_text(local)
    assert "call_local" not in json.dumps(local)
    assert budget.input_tokens <= 700


def test_attachment_only_latest_user_turn_never_falls_back_to_older_text():
    cloud = load_request()
    cloud["input"].append(
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_image", "image_url": "data:image/png;base64,AA"}],
        }
    )

    with pytest.raises(CodexRequestError, match="no textual instruction"):
        build_local_payload(cloud, [], Settings())


def test_active_turn_trimming_omits_old_tool_output_before_new_output():
    items = [
        {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "active task"},
        ]},
        {"type": "function_call", "call_id": "old", "name": "exec", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "old", "output": "OLD " * 4_000},
        {"type": "function_call", "call_id": "new", "name": "exec", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "new", "output": "NEW " * 4_000},
    ]

    trimmed, count = _trim_active_items(items, "active task", budget=5_000)

    assert count == 1
    assert trimmed[2]["output"] == "[output omitted]"
    assert "NEW" in trimmed[4]["output"]


def test_build_local_payload_prunes_stale_same_turn_history_before_413():
    cloud = load_request()
    active = [
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "continue the approved plan"},
            ],
        }
    ]
    for index in range(24):
        active.extend(
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": f"stale-checkpoint-{index} " + "review " * 80,
                        }
                    ],
                },
                {
                    "type": "function_call",
                    "call_id": f"call-{index}",
                    "name": "exec",
                    "arguments": json.dumps(
                        {"source": f"stale-command-{index} " + "x" * 800}
                    ),
                },
                {
                    "type": "function_call_output",
                    "call_id": f"call-{index}",
                    "output": f"stale-output-{index} " + "y" * 2_000,
                },
            ]
        )
    active.append(
        {
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "output_text", "text": "current checkpoint survives"},
            ],
        }
    )
    cloud["input"] = active
    settings = Settings(
        codex_context_window=2_000,
        codex_reply_reserve_tokens=500,
        codex_system_budget_tokens=100,
        codex_memory_budget_tokens=100,
        codex_tools_budget_tokens=100,
        codex_active_turn_budget_tokens=1_000,
    )

    local, budget = build_local_payload(cloud, [], settings)
    rendered = json.dumps(local)

    assert "continue the approved plan" in rendered
    assert "current checkpoint survives" in rendered
    assert "stale-checkpoint-0" not in rendered
    assert "stale-command-0" not in rendered
    assert "stale-command-23" in rendered
    assert budget.input_tokens <= 1_500
    assert budget.trimmed_items > 24


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


def test_build_local_payload_drops_attachment_from_latest_user_item():
    cloud = load_request()
    cloud["input"][4]["content"].append(
        {"type": "input_image", "image_url": "data:image/png;base64," + "A" * 20_000}
    )
    settings = Settings(
        codex_context_window=1_000,
        codex_reply_reserve_tokens=100,
        codex_system_budget_tokens=100,
        codex_memory_budget_tokens=100,
        codex_tools_budget_tokens=100,
        codex_active_turn_budget_tokens=600,
    )

    local, budget = build_local_payload(cloud, [], settings)

    assert "input_image" not in json.dumps(local)
    assert "active task" in content_text(local)
    assert budget.input_tokens <= 900


def test_build_local_payload_drops_small_attachment_before_local_routing():
    cloud = load_request()
    cloud["input"][4]["content"].append(
        {"type": "input_image", "image_url": "data:image/png;base64,AA"}
    )

    local, budget = build_local_payload(cloud, [], Settings())

    assert "input_image" not in json.dumps(local)
    assert budget.trimmed_items == 1


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
