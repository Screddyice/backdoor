import logging
import sqlite3
from pathlib import Path

import pytest

from src.proxy import memory
from src.proxy.config import Settings
from src.proxy.memory_recall import recall_context


def _store(path: Path) -> Path:
    c = sqlite3.connect(path)
    c.executescript(
        """
        CREATE TABLE session_summaries(id INTEGER PRIMARY KEY, request TEXT, investigated TEXT, learned TEXT);
        CREATE VIRTUAL TABLE session_summaries_fts USING fts5(learned, investigated, request, content='session_summaries', content_rowid='id');
        CREATE TABLE user_prompts(id INTEGER PRIMARY KEY, prompt_text TEXT);
        CREATE VIRTUAL TABLE user_prompts_fts USING fts5(prompt_text, content='user_prompts', content_rowid='id');
        INSERT INTO session_summaries VALUES (1, 'tunnel work', 'looked at launchd', 'check the port is free before starting a tunnel');
        INSERT INTO session_summaries_fts(rowid, learned, investigated, request) SELECT id, learned, investigated, request FROM session_summaries;
        INSERT INTO user_prompts VALUES (1, 'the backdoor router listens on 8083');
        INSERT INTO user_prompts_fts(rowid, prompt_text) SELECT id, prompt_text FROM user_prompts;
        """
    )
    c.commit()
    c.close()
    return path


def test_recall_reads_summaries_and_prompts_ranked_and_budgeted(tmp_path):
    db = _store(tmp_path / "claude-mem.db")
    out = memory.recall("starting a tunnel on a port", k=5, char_budget=500, cache=db)
    assert out and "check the port is free" in out[0]
    assert memory.recall("router 8083", k=5, char_budget=500, cache=db) == ["the backdoor router listens on 8083"]
    assert memory.recall("router 8083", k=5, char_budget=10, cache=db) == []


def _store_of(path: Path, texts: list[str]) -> Path:
    """A store whose summaries are the given texts, best-ranked first by insertion."""
    c = sqlite3.connect(path)
    c.executescript(
        """
        CREATE TABLE session_summaries(id INTEGER PRIMARY KEY, request TEXT, investigated TEXT, learned TEXT);
        CREATE VIRTUAL TABLE session_summaries_fts USING fts5(learned, investigated, request, content='session_summaries', content_rowid='id');
        """
    )
    for i, text in enumerate(texts, 1):
        c.execute("INSERT INTO session_summaries VALUES (?, 'tunnel', 'tunnel', ?)", (i, text))
    c.execute(
        "INSERT INTO session_summaries_fts(rowid, learned, investigated, request) "
        "SELECT id, learned, investigated, request FROM session_summaries"
    )
    c.commit()
    c.close()
    return path


def _long(marker: str, size: int = 2000) -> str:
    """A memory shaped like a claude-mem summary: lesson first, then bulk."""
    return f"tunnel {marker} check the port is free before starting a tunnel " + (marker * size)


def test_one_oversized_memory_does_not_discard_the_rest(tmp_path):
    """The loop used to `break` on the first memory that did not fit.

    Everything ranked behind it was thrown away, so a store full of usable
    memories returned an empty list whenever the top hit was long — which is the
    normal shape of a claude-mem summary. An empty list is indistinguishable
    from a store with nothing to say, so nothing surfaced it. Measured against
    the real store on this machine, three ordinary queries returned nothing.
    """
    db = _store_of(tmp_path / "claude-mem.db", [_long("a"), "tunnel: check the port is free before starting a tunnel"])
    out = memory.recall("tunnel", k=5, char_budget=1200, cache=db)
    assert len(out) == 2, "a memory ranked behind an oversized one must still be returned"
    assert sum(len(m) for m in out) <= 1200


def test_the_budget_is_shared_so_one_memory_cannot_take_it_all(tmp_path):
    """Bare mode allows 1200 characters over 6 slots on purpose.

    A single claude-mem summary is 700-1400 characters, so first-come selection
    spends the whole budget on one memory and leaves five slots unused.
    """
    db = _store_of(tmp_path / "claude-mem.db", [_long(chr(ord("a") + i)) for i in range(8)])
    out = memory.recall("tunnel", k=6, char_budget=1200, cache=db)
    assert len(out) == 6, "every slot should be filled when there are candidates for it"
    assert sum(len(m) for m in out) <= 1200
    assert all(m.endswith("\u2026") for m in out), "each was clipped to its share"


def test_a_few_candidates_are_not_clipped_to_a_k_th_of_the_budget(tmp_path):
    """Room left by the slots nothing can fill goes to the memories that exist."""
    db = _store_of(tmp_path / "claude-mem.db", [_long("a"), _long("b")])
    out = memory.recall("tunnel", k=8, char_budget=8000, cache=db)
    assert len(out) == 2
    assert all(not m.endswith("\u2026") for m in out), "both fit whole, so neither should be clipped"


def test_clipping_keeps_the_head_because_the_lesson_comes_first(tmp_path):
    """`learned` is selected before `investigated` and `request`, so the head is the lesson."""
    db = _store_of(tmp_path / "claude-mem.db", [_long(chr(ord("a") + i)) for i in range(6)])
    out = memory.recall("tunnel", k=6, char_budget=1200, cache=db)
    assert all("check the port is free" in m for m in out)


def test_a_budget_too_small_to_say_anything_returns_nothing_and_says_so(tmp_path, caplog):
    """A silent empty result is exactly how the `break` bug survived."""
    db = _store_of(tmp_path / "claude-mem.db", [_long("a")])
    with caplog.at_level(logging.WARNING):
        assert memory.recall("tunnel", k=5, char_budget=10, cache=db) == []
    assert any("candidates matched but none fit" in r.message for r in caplog.records), (
        "matched-but-dropped must be distinguishable from an empty store"
    )


def test_recall_fails_open_on_missing_or_corrupt_store(tmp_path, caplog):
    assert memory.recall("anything", cache=tmp_path / "missing.db") == []
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database")
    with caplog.at_level(logging.WARNING):
        assert memory.recall("anything", cache=bad) == []


@pytest.mark.asyncio
async def test_codex_recall_uses_settings_budget_and_never_raises(tmp_path):
    db = _store(tmp_path / "claude-mem.db")
    settings = Settings(_env_file=None, codex_memory_top_k=3, codex_memory_char_budget=400)
    assert "check the port is free" in (await recall_context("port tunnel", settings, db=db))[0]
    assert await recall_context("port tunnel", settings, db=tmp_path / "nope.db") == []
