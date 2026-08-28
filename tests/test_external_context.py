"""Large fetched documents are externalized before they can fill Qwen's window."""

import json

from src.proxy.config import Settings
from src.proxy.external_context import (
    CONTEXT_OPEN,
    RECALL_OPEN,
    compact_large_tool_results,
    compact_codex_tool_outputs,
    prepare_codex_external_context,
    prepare_external_context,
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

    out, documents = compact_large_tool_results(req, threshold_chars=8_000, char_budget=4_000)

    assert len(documents) == 1
    assert documents[0].source == "https://example.com/report"
    sent = json.dumps(out.model_dump())
    assert CONTEXT_OPEN in sent
    assert "Launch pricing is $49" in sent
    assert len(sent) < len(irrelevant + relevant) / 3
    assert len(str(out.messages[2].content)) < 5_000
    assert len(str(req.messages[2].content)) > 20_000, "the caller's request was mutated"


def test_small_tool_results_are_untouched():
    req = _request("short page")
    out, documents = compact_large_tool_results(req, threshold_chars=8_000, char_budget=4_000)
    assert documents == []
    assert out == req


def test_secret_shaped_documents_are_never_sent_to_cognee():
    assert not safe_to_remember("-----BEGIN PRIVATE KEY-----\nnot-a-real-key")
    assert not safe_to_remember("Authorization: Bearer example-token-value")
    assert safe_to_remember("A public report about product launch pricing.")


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
        payload, threshold_chars=8_000, char_budget=4_000
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
        Settings(external_context_threshold_chars=8_000),
    )

    assert stored[0].text == page
    assert CONTEXT_OPEN in out["input"][-1]["output"]
    assert payload["input"][-1]["output"] == page
