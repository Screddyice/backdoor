"""Exact local transcript storage with lineage-scoped retrieval."""

from concurrent.futures import ThreadPoolExecutor
import json
import stat
import threading

import pytest

from src.proxy.context_store import ContextStore
from src.proxy.models import Message, MessagesRequest


class FakeClock:
    def __init__(self, value: float = 0.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


def messages_request(*texts: str, metadata: dict | None = None) -> MessagesRequest:
    return MessagesRequest(
        model="claude-opus-5",
        messages=[
            Message(
                role="user" if index % 2 == 0 else "assistant",
                content=text,
            )
            for index, text in enumerate(texts)
        ],
        metadata=metadata,
    )


@pytest.fixture
def store(tmp_path):
    return ContextStore(tmp_path / "transcripts.sqlite3")


def test_archive_uses_wal_private_files_and_exact_json(tmp_path):
    path = tmp_path / "private" / "transcripts.sqlite3"
    store = ContextStore(path)

    lineage = store.archive_request(
        messages_request("café", metadata={"ignored": "metadata-secret"})
    )
    stored = store.segments(lineage.lineage_id)

    assert json.loads(stored[0].exact_json) == {
        "content": "café",
        "role": "user",
    }
    assert "metadata-secret" not in path.read_bytes().decode("utf-8", errors="ignore")
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.journal_mode() == "wal"


def test_new_turn_extends_one_unambiguous_lineage(store):
    first = store.archive_request(messages_request("question", "answer"))
    extended = store.archive_request(messages_request("question", "answer", "next"))

    assert extended.lineage_id == first.lineage_id
    assert extended.parent_id is None
    assert extended.matched_prefix == 2
    assert [segment.searchable_text for segment in store.segments(first.lineage_id)] == [
        "question",
        "answer",
        "next",
    ]


def test_fork_inherits_prefix_and_search_never_crosses_lineages(store):
    left = store.archive_request(messages_request("shared", "left-secret"))
    right = store.archive_request(messages_request("shared", "right-secret"))

    assert left.lineage_id != right.lineage_id
    assert right.parent_id == left.lineage_id
    assert right.matched_prefix == 1
    assert store.search(left.lineage_id, "right-secret") == []
    assert [segment.searchable_text for segment in store.search(right.lineage_id, "right-secret")] == [
        "right-secret"
    ]


def test_equal_prefix_tie_creates_a_false_split(store):
    first = store.archive_request(messages_request("same", "left"))
    second = store.archive_request(messages_request("same", "right"))
    third = store.archive_request(messages_request("same", "third"))

    assert len({first.lineage_id, second.lineage_id, third.lineage_id}) == 3
    assert third.parent_id is None
    assert third.matched_prefix == 1


def test_identical_retry_reuses_a_unique_lineage(store):
    first = store.archive_request(messages_request("same", "answer"))
    retry = store.archive_request(messages_request("same", "answer"))

    assert retry.lineage_id == first.lineage_id
    assert len(store.segments(first.lineage_id)) == 2


def test_search_sanitizes_fts_operators_and_punctuation(store):
    lineage = store.archive_request(
        messages_request("the rollback file is /tmp/router-state.json")
    )

    found = store.search(
        lineage.lineage_id,
        "what's /tmp/router-state.json? (AND OR NOT) -- ;",
    )

    assert [segment.searchable_text for segment in found] == [
        "the rollback file is /tmp/router-state.json"
    ]


def test_concurrent_writers_do_not_mix_lineages(tmp_path):
    path = tmp_path / "transcripts.sqlite3"
    stores = [ContextStore(path), ContextStore(path)]
    barrier = threading.Barrier(2)

    def archive(index: int):
        barrier.wait()
        return stores[index].archive_request(
            messages_request("shared", f"suffix-{index}")
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        lineages = list(pool.map(archive, range(2)))

    assert lineages[0].lineage_id != lineages[1].lineage_id
    for index, lineage in enumerate(lineages):
        text = " ".join(
            segment.searchable_text
            for segment in stores[index].segments(lineage.lineage_id)
        )
        assert f"suffix-{index}" in text
        assert f"suffix-{1 - index}" not in text


def test_lineage_survives_store_restart(tmp_path):
    path = tmp_path / "transcripts.sqlite3"
    first_store = ContextStore(path)
    lineage = first_store.archive_request(messages_request("remember this"))

    reopened = ContextStore(path)

    assert reopened.archive_request(messages_request("remember this")).lineage_id == lineage.lineage_id
    assert reopened.segments(lineage.lineage_id)[0].searchable_text == "remember this"


def test_completed_response_expires(store):
    store.put_cached_response("abc", {"id": "msg_1"}, expires_at=20.0)

    assert store.get_cached_response("abc", now=19.0) == {"id": "msg_1"}
    assert store.get_cached_response("abc", now=20.0) is None


def test_prune_runs_only_over_cap_and_keeps_active_lineage(tmp_path, monkeypatch):
    clock = FakeClock()
    store = ContextStore(
        tmp_path / "transcripts.sqlite3",
        max_bytes=1,
        inactive_days=30,
        now_fn=clock,
    )
    old = store.archive_request(messages_request("old lineage"))
    clock.value = 31 * 86_400
    active = store.archive_request(messages_request("active lineage"))
    monkeypatch.setattr(store, "database_size", lambda: 2)

    assert store.prune_if_needed() == 1
    assert store.segments(old.lineage_id) == []
    assert store.segments(active.lineage_id)


def test_prune_does_nothing_below_soft_cap(tmp_path, monkeypatch):
    clock = FakeClock()
    store = ContextStore(
        tmp_path / "transcripts.sqlite3",
        max_bytes=100,
        inactive_days=30,
        now_fn=clock,
    )
    lineage = store.archive_request(messages_request("keep"))
    clock.value = 31 * 86_400
    monkeypatch.setattr(store, "database_size", lambda: 99)

    assert store.prune_if_needed() == 0
    assert store.segments(lineage.lineage_id)
