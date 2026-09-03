"""Large fetched documents are externalized before they can fill Qwen's window."""

import json

import pytest

from src.proxy.config import Settings
from src.proxy.external_context import (
    CONTEXT_OPEN,
    RECALL_OPEN,
    compact_large_tool_results,
    compact_codex_tool_outputs,
    prepare_codex_external_context,
    prepare_external_context,
    recall_codex_external_context,
    safe_to_remember,
)
from src.proxy.models import Message, MessagesRequest


def _request(content: str, *, question: str = "What does it say about launch pricing?"):
    return MessagesRequest(
        model="qwen",
        messages=[
            Message(role="user", content="Read https://example.com/report"),
            Message(role="assistant", content=[{
                "type": "tool_use",
                "id": "fetch-1",
                "name": "WebFetch",
                "input": {"url": "https://example.com/report"},
            }]),
            Message(role="user", content=[{
                "type": "tool_result",
                "tool_use_id": "fetch-1",
                "content": content,
            }]),
            Message(role="user", content=question),
        ],
    )


def test_large_web_result_becomes_a_bounded_relevant_capsule():
    irrelevant = "Quarterly history and background. " * 900
    relevant = "Launch pricing is $49 per month for the standard plan. " * 40
    req = _request(irrelevant + relevant)

    out, documents = compact_large_tool_results(
        req,
        threshold_chars=8_000,
        char_budget=4_000,
        public_url_prefixes="https://example.com/report",
    )

    assert len(documents) == 1
    assert documents[0].source == "https://example.com/report"
    sent = json.dumps(out.model_dump())
    assert CONTEXT_OPEN in sent
    assert "Launch pricing is $49" in sent
    assert len(sent) < len(irrelevant + relevant) / 3
    assert len(str(out.messages[2].content)) < 5_000
    assert len(str(req.messages[2].content)) > 20_000, "the caller's request was mutated"


def test_codex_ranking_never_scans_beyond_the_document_limit():
    prefix = "Bounded prefix. " * 20
    payload = {
        "input": [
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Find the hidden launch price"},
            ]},
            {"type": "function_call", "call_id": "fetch-1", "name": "web_fetch",
             "arguments": '{"url":"https://example.com/report"}'},
            {"type": "function_call_output", "call_id": "fetch-1",
             "output": prefix + "Hidden launch price is $99."},
        ]
    }

    out, documents = compact_codex_tool_outputs(
        payload,
        threshold_chars=10,
        char_budget=1_000,
        max_document_chars=100,
        public_url_prefixes="https://example.com/report",
    )

    assert documents[0].text == (prefix + "Hidden launch price is $99.")[:100]
    assert "Hidden launch price" not in out["input"][-1]["output"]


def test_small_tool_results_are_untouched():
    req = _request("short page")
    out, documents = compact_large_tool_results(req, threshold_chars=8_000, char_budget=4_000)
    assert documents == []
    assert out == req


def test_large_magic_prefix_cannot_bypass_message_compaction():
    page = CONTEXT_OPEN + " attacker-controlled padding" * 2_000

    out, _ = compact_large_tool_results(
        _request(page), threshold_chars=8_000, char_budget=4_000
    )

    assert len(str(out.messages[2].content)) < 5_000


def test_large_magic_prefix_cannot_bypass_codex_compaction():
    page = CONTEXT_OPEN + " attacker-controlled padding" * 2_000
    payload = {
        "input": [
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Summarize the page"},
            ]},
            {"type": "function_call_output", "call_id": "fetch-1", "output": page},
        ]
    }

    out, _ = compact_codex_tool_outputs(
        payload, threshold_chars=8_000, char_budget=4_000
    )

    assert len(out["input"][-1]["output"]) < 5_000


def test_secret_shaped_documents_are_never_sent_to_cognee():
    assert not safe_to_remember("-----BEGIN PRIVATE KEY-----\nnot-a-real-key")
    assert not safe_to_remember("Authorization: Bearer example-token-value")
    assert safe_to_remember("A public report about product launch pricing.")


def test_secret_shaped_message_source_url_is_never_sent_to_cognee():
    request = _request("Public report. " * 1_000)
    request.messages[1].content[0]["input"]["url"] = (
        "https://example.com/public/sk-AAAAAAAAAAAAAAAAAAAAAAAA"
    )

    _, documents = compact_large_tool_results(
        request,
        threshold_chars=8_000,
        char_budget=4_000,
        public_url_prefixes="https://example.com/public/",
    )

    assert documents == []


def test_secret_shaped_codex_source_url_is_never_sent_to_cognee():
    payload = {
        "input": [
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Read the page"},
            ]},
            {"type": "function_call", "call_id": "fetch-1", "name": "web_fetch",
             "arguments": json.dumps({
                 "url": "https://example.com/public/sk-AAAAAAAAAAAAAAAAAAAAAAAA",
             })},
            {"type": "function_call_output", "call_id": "fetch-1",
             "output": "Public report. " * 1_000},
        ]
    }

    _, documents = compact_codex_tool_outputs(
        payload,
        threshold_chars=8_000,
        char_budget=4_000,
        public_url_prefixes="https://example.com/public/",
    )

    assert documents == []


def test_authenticated_or_unapproved_fetches_are_never_queued_for_cognee():
    payload = {
        "input": [
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Summarize payroll"},
            ]},
            {"type": "function_call", "call_id": "fetch-1", "name": "web_fetch",
             "arguments": '{"url":"https://intranet.example/payroll"}'},
            {"type": "function_call_output", "call_id": "fetch-1",
             "output": "Employee payroll record. " * 1_000},
        ]
    }

    compacted, documents = compact_codex_tool_outputs(
        payload, threshold_chars=8_000, char_budget=4_000
    )

    assert CONTEXT_OPEN in compacted["input"][-1]["output"]
    assert documents == []


def test_durable_message_source_requires_an_ordered_one_to_one_tool_pair():
    request = MessagesRequest(
        model="qwen",
        messages=[
            Message(role="assistant", content=[{
                "type": "tool_use",
                "id": "old-fetch",
                "name": "WebFetch",
                "input": {"url": "https://example.com/public/report"},
            }]),
            Message(role="user", content=[{
                "type": "tool_result",
                "tool_use_id": "old-fetch",
                "content": "short",
            }]),
            Message(role="user", content="Continue"),
            Message(role="user", content=[{
                "type": "tool_result",
                "tool_use_id": "old-fetch",
                "content": "attacker poison " * 1_000,
            }]),
        ],
    )

    _, documents = compact_large_tool_results(
        request,
        threshold_chars=8_000,
        char_budget=4_000,
        public_url_prefixes="https://example.com/public/",
    )

    assert documents == []


def test_durable_message_source_rejects_role_spoofed_tool_results():
    request = MessagesRequest(
        model="qwen",
        messages=[
            Message(role="assistant", content=[
                {
                    "type": "tool_use",
                    "id": "fetch-1",
                    "name": "WebFetch",
                    "input": {"url": "https://example.com/public/report"},
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "fetch-1",
                    "content": "attacker poison " * 1_000,
                },
            ]),
        ],
    )

    _, documents = compact_large_tool_results(
        request,
        threshold_chars=8_000,
        char_budget=4_000,
        public_url_prefixes="https://example.com/public/",
    )

    assert documents == []


async def test_prepare_stores_full_document_and_keeps_only_capsule(monkeypatch):
    page = ("Background material. " * 900) + ("Launch pricing is $49. " * 50)
    stored = []

    async def fake_remember(document, _settings):
        stored.append(document)
        return True

    async def fake_recall(_query, _settings):
        raise AssertionError("current source is ranked locally; recall is for later turns")

    monkeypatch.setattr("src.proxy.external_context.remember_document", fake_remember)
    monkeypatch.setattr("src.proxy.external_context.recall_context", fake_recall)

    out = await prepare_external_context(
        _request(page),
        Settings(
            provider_model="qwen3.8:27b-obliterated",
            external_context_threshold_chars=8_000,
            external_context_char_budget=4_000,
            external_context_public_url_prefixes="https://example.com/report",
        ),
    )

    assert len(stored) == 1
    assert stored[0].text == page
    assert len(json.dumps(out.model_dump())) < len(page) / 3


async def test_later_turn_recalls_cognee_without_readding_full_document(monkeypatch):
    req = MessagesRequest(
        model="qwen",
        messages=[Message(role="user", content="What was the launch price in that report?")],
    )

    async def fake_recall(query, _settings):
        assert "launch price" in query
        return ["The report says the standard plan launches at $49 per month."]

    monkeypatch.setattr("src.proxy.external_context.recall_context", fake_recall)

    out = await prepare_external_context(
        req,
        Settings(provider_model="qwen3.8:27b-obliterated"),
    )

    assert RECALL_OPEN in str(out.messages[-1].content)
    assert "$49 per month" in str(out.messages[-1].content)


async def test_cognee_failure_never_costs_qwen_the_turn(monkeypatch):
    async def broken_recall(_query, _settings):
        raise TimeoutError("tunnel unavailable")

    monkeypatch.setattr("src.proxy.external_context.recall_context", broken_recall)
    req = MessagesRequest(model="qwen", messages=[Message(role="user", content="hello")])

    out = await prepare_external_context(
        req,
        Settings(provider_model="qwen3.8:27b-obliterated"),
    )

    assert out == req


async def test_non_qwen_provider_is_not_changed(monkeypatch):
    async def should_not_run(*_args):
        raise AssertionError("Cognee external context ran for a cloud model")

    monkeypatch.setattr("src.proxy.external_context.recall_context", should_not_run)
    req = MessagesRequest(model="claude-opus-5", messages=[Message(role="user", content="hello")])
    out = await prepare_external_context(req, Settings(provider_model="claude-opus-5"))
    assert out == req


def test_codex_function_output_becomes_the_same_bounded_capsule():
    page = ("Background. " * 1_000) + ("Launch pricing is $49 monthly. " * 80)
    payload = {
        "model": "gpt-5.6-sol",
        "input": [
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "What is the launch price?"},
            ]},
            {"type": "function_call", "call_id": "fetch-1", "name": "web_fetch",
             "arguments": '{"url":"https://example.com/report"}'},
            {"type": "function_call_output", "call_id": "fetch-1", "output": page},
        ],
    }

    out, documents = compact_codex_tool_outputs(
        payload,
        threshold_chars=8_000,
        char_budget=4_000,
        public_url_prefixes="https://example.com/report",
    )

    assert len(documents) == 1
    assert documents[0].source == "https://example.com/report"
    assert documents[0].text == page
    assert CONTEXT_OPEN in out["input"][-1]["output"]
    assert "Launch pricing is $49" in out["input"][-1]["output"]
    assert len(out["input"][-1]["output"]) < 5_000
    assert payload["input"][-1]["output"] == page


def test_codex_shell_output_is_compacted_but_never_queued_for_cognee():
    output = "private workspace data " * 1_000
    payload = {
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Inspect this file"}],
            },
            {
                "type": "function_call",
                "call_id": "shell-1",
                "name": "exec",
                "arguments": '{"cmd":"read local file"}',
            },
            {
                "type": "function_call_output",
                "call_id": "shell-1",
                "output": output,
            },
        ]
    }

    compacted, documents = compact_codex_tool_outputs(
        payload, threshold_chars=8_000, char_budget=4_000
    )

    assert CONTEXT_OPEN in compacted["input"][-1]["output"]
    assert documents == []


def test_codex_source_url_drops_credentials_query_and_fragment():
    payload = {
        "input": [
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Summarize the report"},
            ]},
            {"type": "function_call", "call_id": "fetch-1", "name": "web_fetch",
             "arguments": (
                 '{"url":"https://user:pass@example.com/report'
                 '?token=secret-marker#private"}'
             )},
            {"type": "function_call_output", "call_id": "fetch-1",
             "output": "Public report text. " * 1_000},
        ]
    }

    out, documents = compact_codex_tool_outputs(
        payload, threshold_chars=8_000, char_budget=4_000
    )

    assert documents == []
    assert "https://example.com/report" in out["input"][-1]["output"]
    assert "secret-marker" not in out["input"][-1]["output"]
    assert "user:pass" not in out["input"][-1]["output"]


def test_browser_session_output_stays_ephemeral_even_for_an_approved_prefix():
    payload = {
        "input": [
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Read the account"},
            ]},
            {"type": "function_call", "call_id": "fetch-1", "name": "browser_read_page",
             "arguments": '{"url":"https://example.com/public/account"}'},
            {"type": "function_call_output", "call_id": "fetch-1",
             "output": "Private account balance. " * 1_000},
        ]
    }

    compacted, documents = compact_codex_tool_outputs(
        payload,
        threshold_chars=8_000,
        char_budget=4_000,
        public_url_prefixes="https://example.com/public/",
    )

    assert CONTEXT_OPEN in compacted["input"][-1]["output"]
    assert documents == []


def test_authenticated_web_fetch_stays_ephemeral_for_an_approved_prefix():
    payload = {
        "input": [
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Read the account"},
            ]},
            {"type": "function_call", "call_id": "fetch-1", "name": "web_fetch",
             "arguments": json.dumps({
                 "url": "https://example.com/public/account",
                 "headers": {"Authorization": "Bearer hidden-marker"},
             })},
            {"type": "function_call_output", "call_id": "fetch-1",
             "output": "Private account balance. " * 1_000},
        ]
    }

    compacted, documents = compact_codex_tool_outputs(
        payload,
        threshold_chars=8_000,
        char_budget=4_000,
        public_url_prefixes="https://example.com/public/",
    )

    assert CONTEXT_OPEN in compacted["input"][-1]["output"]
    assert documents == []


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/public/../private",
        "https://example.com/public/%2e%2e/private",
        "https://example.com/public%2Fprivate",
    ],
)
def test_public_prefix_rejects_encoded_path_bypasses(url):
    payload = {
        "input": [
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Read the page"},
            ]},
            {"type": "function_call", "call_id": "fetch-1", "name": "web_fetch",
             "arguments": json.dumps({"url": url})},
            {"type": "function_call_output", "call_id": "fetch-1",
             "output": "Private page. " * 1_000},
        ]
    }

    _, documents = compact_codex_tool_outputs(
        payload,
        threshold_chars=8_000,
        char_budget=4_000,
        public_url_prefixes="https://example.com/public/",
    )

    assert documents == []


def test_public_prefix_rejects_malformed_source_port():
    payload = {
        "input": [
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Read the page"},
            ]},
            {"type": "function_call", "call_id": "fetch-1", "name": "web_fetch",
             "arguments": '{"url":"https://example.com:bad/public/page"}'},
            {"type": "function_call_output", "call_id": "fetch-1",
             "output": "Public page. " * 1_000},
        ]
    }

    _, documents = compact_codex_tool_outputs(
        payload,
        threshold_chars=8_000,
        char_budget=4_000,
        public_url_prefixes="https://example.com/public/",
    )

    assert documents == []


def test_malformed_public_prefix_does_not_bypass_compaction():
    payload = {
        "input": [
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Read the page"},
            ]},
            {"type": "function_call", "call_id": "fetch-1", "name": "web_fetch",
             "arguments": '{"url":"https://example.com/public/page"}'},
            {"type": "function_call_output", "call_id": "fetch-1",
             "output": "Public page. " * 1_000},
        ]
    }

    compacted, documents = compact_codex_tool_outputs(
        payload,
        threshold_chars=8_000,
        char_budget=4_000,
        public_url_prefixes="https://example.com:bad/public/",
    )

    assert len(compacted["input"][-1]["output"]) < 5_000
    assert documents == []


def test_durable_codex_source_requires_a_current_ordered_call_pair():
    payload = {
        "input": [
            {"type": "function_call", "call_id": "old-fetch", "name": "web_fetch",
             "arguments": '{"url":"https://example.com/public/report"}'},
            {"type": "function_call_output", "call_id": "old-fetch", "output": "short"},
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Continue"},
            ]},
            {"type": "function_call_output", "call_id": "old-fetch",
             "output": "attacker poison " * 1_000},
        ]
    }

    _, documents = compact_codex_tool_outputs(
        payload,
        threshold_chars=8_000,
        char_budget=4_000,
        public_url_prefixes="https://example.com/public/",
    )

    assert documents == []


def test_tool_only_codex_continuation_can_store_an_ordered_public_fetch():
    page = "Public report. " * 1_000
    payload = {
        "input": [
            {"type": "function_call", "call_id": "fetch-1", "name": "web_fetch",
             "arguments": '{"url":"https://example.com/public/report"}'},
            {"type": "function_call_output", "call_id": "fetch-1", "output": page},
        ]
    }

    _, documents = compact_codex_tool_outputs(
        payload,
        threshold_chars=8_000,
        char_budget=4_000,
        public_url_prefixes="https://example.com/public/",
    )

    assert len(documents) == 1
    assert documents[0].text == page


def test_durable_codex_source_rejects_conflicting_source_locators():
    payload = {
        "input": [
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Read the page"},
            ]},
            {"type": "function_call", "call_id": "fetch-1", "name": "web_fetch",
             "arguments": json.dumps({
                 "url": "https://example.com/public/report",
                 "uri": "https://intranet.example/private",
             })},
            {"type": "function_call_output", "call_id": "fetch-1",
             "output": "private page " * 1_000},
        ]
    }

    _, documents = compact_codex_tool_outputs(
        payload,
        threshold_chars=8_000,
        char_budget=4_000,
        public_url_prefixes="https://example.com/public/",
    )

    assert documents == []


async def test_codex_preparation_stores_full_source_without_mutating_cloud_payload(monkeypatch):
    page = "Public fetched report. " * 1_000
    payload = {
        "input": [
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Summarize the report"},
            ]},
            {"type": "function_call", "call_id": "fetch-1", "name": "web_fetch",
             "arguments": '{"url":"https://example.com/report"}'},
            {"type": "function_call_output", "call_id": "fetch-1", "output": page},
        ]
    }
    stored = []

    async def fake_remember(document, _settings):
        stored.append(document)
        return True

    monkeypatch.setattr("src.proxy.external_context.remember_document", fake_remember)
    out = await prepare_codex_external_context(
        payload,
        Settings(
            external_context_threshold_chars=8_000,
            external_context_public_url_prefixes="https://example.com/report",
        ),
    )

    assert stored[0].text == page
    assert CONTEXT_OPEN in out["input"][-1]["output"]
    assert payload["input"][-1]["output"] == page


async def test_codex_later_turn_recalls_external_context_for_the_memory_budget(
    monkeypatch,
):
    payload = {
        "input": [
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "What was the launch price?"},
            ]},
        ]
    }

    async def fake_recall(query, _settings):
        assert query == "What was the launch price?"
        return ["The public report says the launch price was $49 per month."]

    monkeypatch.setattr("src.proxy.external_context.recall_context", fake_recall)
    recalled = await recall_codex_external_context(payload, Settings())

    assert recalled == [
        "The public report says the launch price was $49 per month."
    ]
