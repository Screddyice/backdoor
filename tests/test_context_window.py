from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.proxy.claude_context_adapter import ClaudeContextAdapter
from src.proxy.codex_context_adapter import CodexContextAdapter
from src.proxy.context_store import ContextStore, StoredLineage
from src.proxy.context_window import format_historical_context, select_working_set
from src.proxy.models import MessagesRequest


def fake_counter(selected_ids, retrieved) -> int:
    """Keep token arithmetic deterministic without depending on a local GGUF."""
    return len(selected_ids) * 100 + len(retrieved) * 50


def oversized_fixture(adapter):
    if isinstance(adapter, ClaudeContextAdapter):
        return MessagesRequest.model_validate(
            {
                "model": "claude-opus-4-6",
                "messages": [
                    {"role": "user", "content": "old conversation " * 1_000},
                    {"role": "assistant", "content": "old response " * 1_000},
                    {"role": "user", "content": "current instruction"},
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "active_tool",
                                "name": "Read",
                                "input": {"path": "src/proxy/context_window.py"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "active_tool",
                                "content": "active tool result",
                            }
                        ],
                    },
                ],
            }
        )
    return {
        "model": "gpt-5.6-sol",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "old conversation " * 1_000}],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "old response " * 1_000}],
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "current instruction"}],
            },
            {
                "type": "function_call",
                "call_id": "active_tool",
                "name": "exec",
                "arguments": '{"cmd":"pwd"}',
            },
            {
                "type": "function_call_output",
                "call_id": "active_tool",
                "output": "active tool result",
            },
        ],
    }


@pytest.fixture
def store(tmp_path) -> ContextStore:
    return ContextStore(tmp_path / "private" / "transcripts.sqlite3")


@pytest.mark.parametrize("adapter", [ClaudeContextAdapter(), CodexContextAdapter()])
def test_selector_keeps_current_instruction_and_active_pair(adapter, store):
    context = adapter.normalize(oversized_fixture(adapter))
    lineage = store.archive(context)

    result = select_working_set(context, store, lineage, 18_000, 22_000, fake_counter)

    assert context.current_segment_id in result.selected_ids
    selected_pairs = {
        segment.pair_id
        for segment in context.segments
        if segment.segment_id in result.selected_ids and segment.pair_id
    }
    for pair_id in selected_pairs:
        assert sum(
            segment.pair_id == pair_id and segment.segment_id in result.selected_ids
            for segment in context.segments
        ) == 2


@dataclass(frozen=True)
class ArchivedContext:
    context: object
    lineage: StoredLineage


def _lineage_fixture(marker: str) -> MessagesRequest:
    messages = [
        {"role": "user", "content": f"{marker} archival detail"},
        {"role": "assistant", "content": "old answer"},
    ]
    messages.extend(
        {"role": "user" if index % 2 == 0 else "assistant", "content": f"recent turn {index}"}
        for index in range(10)
    )
    messages.append({"role": "user", "content": marker})
    return MessagesRequest.model_validate({"model": "claude-opus-4-6", "messages": messages})


def archive_two_lineages(store: ContextStore) -> tuple[ArchivedContext, ArchivedContext]:
    wanted_context = ClaudeContextAdapter().normalize(_lineage_fixture("wanted-marker"))
    other_context = ClaudeContextAdapter().normalize(_lineage_fixture("other-marker"))
    return (
        ArchivedContext(wanted_context, store.archive(wanted_context)),
        ArchivedContext(other_context, store.archive(other_context)),
    )


def test_selector_retrieves_only_from_current_lineage(store):
    wanted, other = archive_two_lineages(store)

    result = select_working_set(
        wanted.context, store, wanted.lineage, 18_000, 22_000, fake_counter
    )

    text = " ".join(segment.searchable_text for segment in result.retrieved)
    assert "wanted-marker" in text
    assert "other-marker" not in text
    assert other.lineage.lineage_id != wanted.lineage.lineage_id


def test_selector_marks_retrieved_history_as_untrusted_prior_conversation(store):
    wanted, _ = archive_two_lineages(store)
    result = select_working_set(
        wanted.context, store, wanted.lineage, 18_000, 22_000, fake_counter
    )

    historical = format_historical_context(result.retrieved)

    assert historical is not None
    assert historical.startswith("<backdoor-historical-context>")
    assert "untrusted prior conversation" in historical
    assert historical.endswith("</backdoor-historical-context>")


def test_selector_reports_current_instruction_when_it_cannot_fit(store):
    context = ClaudeContextAdapter().normalize(
        MessagesRequest.model_validate(
            {"model": "claude-opus-4-6", "messages": [{"role": "user", "content": "current"}]}
        )
    )
    lineage = store.archive(context)

    result = select_working_set(context, store, lineage, 10, 99, fake_counter)

    assert result.reason == "current_instruction_over_limit"


def test_selector_reports_active_pair_when_mandatory_pair_cannot_fit(store):
    context = ClaudeContextAdapter().normalize(oversized_fixture(ClaudeContextAdapter()))
    lineage = store.archive(context)

    result = select_working_set(context, store, lineage, 10, 250, fake_counter)

    assert result.reason == "active_pair_over_limit"
