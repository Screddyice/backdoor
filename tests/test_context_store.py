import json
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.proxy.claude_context_adapter import ClaudeContextAdapter
from src.proxy.codex_context_adapter import CodexContextAdapter
from src.proxy.context_store import ContextStore
from src.proxy.models import MessagesRequest


class Clock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def claude_fixture(current: str = "active task") -> MessagesRequest:
    return MessagesRequest.model_validate(
        {
            "model": "claude-opus-4-6",
            "messages": [
                {"role": "user", "content": "shared conversation"},
                {"role": "assistant", "content": "previous answer"},
                {"role": "user", "content": current},
            ],
        }
    )


def repeated_claude_fixture() -> MessagesRequest:
    return MessagesRequest.model_validate(
        {
            "model": "claude-opus-4-6",
            "messages": [
                {"role": "user", "content": "shared conversation"},
                {"role": "assistant", "content": "same response"},
                {"role": "assistant", "content": "same response"},
                {"role": "user", "content": "active task"},
            ],
        }
    )


def codex_fixture(current: str = "codex-only") -> dict:
    return {
        "model": "gpt-5.6",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "shared conversation"}],
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "previous answer"}],
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": current}],
            },
        ],
    }


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path, clock) -> ContextStore:
    return ContextStore(
        tmp_path / "private" / "transcripts.sqlite3",
        max_bytes=1_024,
        inactive_days=30,
        clock=clock,
    )


def archive_at(store: ContextStore, clock: Clock, at: float, current: str):
    clock.value = at
    return store.archive(ClaudeContextAdapter().normalize(claude_fixture(current)))


def test_archive_uses_private_wal_and_exact_segments(tmp_path):
    """A private archive must retain exact request-body content and restrictive modes."""
    path = tmp_path / "private" / "transcripts.sqlite3"

    store = ContextStore(path)
    lineage = store.archive(ClaudeContextAdapter().normalize(claude_fixture()))
    stored = store.segments(lineage.lineage_id)

    assert json.loads(stored[-1].exact_json) == {"content": "active task", "role": "user"}
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.journal_mode() == "wal"


def test_archive_keeps_occurrences_while_deduplicating_segment_bodies(store):
    """Repeated content needs separate selection IDs without duplicate stored bodies."""
    context = ClaudeContextAdapter().normalize(repeated_claude_fixture())

    lineage = store.archive(context)
    stored = store.segments(lineage.lineage_id)

    repeated = [segment for segment in stored if segment.searchable_text == "same response"]
    assert tuple(segment.segment_id for segment in stored) == lineage.segment_ids
    assert [segment.segment_id for segment in repeated] == [
        segment.segment_id for segment in context.segments if segment.searchable_text == "same response"
    ]
    assert repeated[0].segment_id != repeated[1].segment_id
    with sqlite3.connect(store.path) as connection:
        body_count = connection.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
    assert body_count == 3


def test_lineages_never_cross_client_kinds(store):
    """A shared transcript prefix in another client protocol cannot become a parent."""
    claude = store.archive(ClaudeContextAdapter().normalize(claude_fixture("claude-only")))
    codex = store.archive(CodexContextAdapter().normalize(codex_fixture("codex-only")))

    assert claude.lineage_id != codex.lineage_id
    assert store.search(claude.lineage_id, "codex-only", 6, ()) == []


def test_divergence_creates_child_without_mixing_suffixes(store):
    """A branch inherits only its common prefix and keeps the sibling suffix out."""
    left = store.archive(ClaudeContextAdapter().normalize(claude_fixture("left-secret")))
    right = store.archive(ClaudeContextAdapter().normalize(claude_fixture("right-secret")))

    assert right.lineage_id != left.lineage_id
    assert right.parent_id == left.lineage_id
    assert right.matched_prefix == 2
    assert "left-secret" not in " ".join(segment.searchable_text for segment in store.segments(right.lineage_id))


def test_ambiguous_prefix_creates_independent_lineage(store):
    """Equal-length sibling candidates cannot silently choose one task as parent."""
    first = store.archive(ClaudeContextAdapter().normalize(claude_fixture("left-secret")))
    second = store.archive(ClaudeContextAdapter().normalize(claude_fixture("right-secret")))

    ambiguous = store.archive(ClaudeContextAdapter().normalize(claude_fixture("third-secret")))

    assert ambiguous.parent_id is None
    assert ambiguous.lineage_id not in {first.lineage_id, second.lineage_id}
    assert ambiguous.matched_prefix == 0


def test_search_is_lineage_scoped_limited_and_excludes_occurrences(store):
    """FTS retrieval cannot cross a lineage or reselect the caller's occurrence."""
    wanted = store.archive(ClaudeContextAdapter().normalize(claude_fixture("wanted marker")))
    other = store.archive(ClaudeContextAdapter().normalize(claude_fixture("other marker")))
    wanted_marker = store.segments(wanted.lineage_id)[-1]

    hits = store.search(wanted.lineage_id, "marker", 1, ())
    assert [segment.segment_id for segment in hits] == [wanted_marker.segment_id]

    hits = store.search(wanted.lineage_id, "marker", 1, {wanted_marker.segment_id})

    assert hits == []
    assert store.search(other.lineage_id, "wanted", 6, ()) == []
    assert store.search(wanted.lineage_id, "", 6, ()) == []
    assert store.search(wanted.lineage_id, 'marker" OR *', 6, ()) == []


def test_archive_handles_concurrent_calls_without_losing_lineage_membership(store):
    """Concurrent local requests must finish with one complete lineage rather than lock errors."""
    context = ClaudeContextAdapter().normalize(claude_fixture("concurrent marker"))

    with ThreadPoolExecutor(max_workers=4) as executor:
        lineages = list(executor.map(lambda _: store.archive(context), range(8)))

    assert {lineage.lineage_id for lineage in lineages} == {lineages[0].lineage_id}
    assert len(store.segments(lineages[0].lineage_id)) == len(context.segments)


def test_cached_response_expires(store):
    """An expired response must not be reused for a retry."""
    store.put_cached_response("key", {"ok": True}, expires_at=20.0)

    assert store.get_cached_response("key", now=19.0) == {"ok": True}
    assert store.get_cached_response("key", now=20.0) is None


def test_prune_keeps_active_lineage_and_removes_old_inactive(store, clock):
    """The soft cap removes only lineages inactive longer than the retention window."""
    old = archive_at(store, clock, 0, "old")
    active = archive_at(store, clock, 31 * 86_400, "active")
    store.database_size = lambda: store.max_bytes + 1

    assert store.prune_if_needed(now=clock.value) == 1
    assert store.segments(old.lineage_id) == []
    assert store.segments(active.lineage_id)
