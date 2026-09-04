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
    c.commit(); c.close()
    return path


def test_recall_reads_summaries_and_prompts_ranked_and_budgeted(tmp_path):
    db = _store(tmp_path / "claude-mem.db")
    out = memory.recall("starting a tunnel on a port", k=5, char_budget=500, cache=db)
    assert out and "check the port is free" in out[0]
    assert memory.recall("router 8083", k=5, char_budget=500, cache=db) == ["the backdoor router listens on 8083"]
    assert memory.recall("router 8083", k=5, char_budget=10, cache=db) == []


def _store_with_a_long_memory(path: Path) -> Path:
    """A store whose best-ranked hit is longer than a normal budget.

    This is the ordinary case, not a corner: claude-mem session summaries run
    to a few thousand characters while the router's default budget is 1200.
    """
    c = sqlite3.connect(path)
    long_text = "tunnel " + ("x" * 2000)
    c.executescript(
        """
        CREATE TABLE session_summaries(id INTEGER PRIMARY KEY, request TEXT, investigated TEXT, learned TEXT);
        CREATE VIRTUAL TABLE session_summaries_fts USING fts5(learned, investigated, request, content='session_summaries', content_rowid='id');
        CREATE TABLE user_prompts(id INTEGER PRIMARY KEY, prompt_text TEXT);
        CREATE VIRTUAL TABLE user_prompts_fts USING fts5(prompt_text, content='user_prompts', content_rowid='id');
        """
    )
    c.execute("INSERT INTO session_summaries VALUES (1, 'tunnel', 'tunnel', ?)", (long_text,))
    c.execute("INSERT INTO session_summaries VALUES (2, 'tunnel', 'tunnel', 'check the port is free before starting a tunnel')")
    c.execute("INSERT INTO session_summaries_fts(rowid, learned, investigated, request) SELECT id, learned, investigated, request FROM session_summaries")
    c.commit(); c.close()
    return path


def test_one_oversized_memory_does_not_discard_the_rest(tmp_path):
    """The loop used to `break` on the first memory that did not fit.

    Everything ranked behind it was thrown away, so a store full of usable
    memories returned an empty list whenever the top hit was long — which is
    the normal shape of a claude-mem summary. An empty list is indistinguishable
    from a store with nothing to say, so nothing surfaced it.
    """
    db = _store_with_a_long_memory(tmp_path / "claude-mem.db")
    out = memory.recall("tunnel", k=5, char_budget=1200, cache=db)
    assert out, "a fitting memory ranked behind an oversized one must still be returned"
    assert any("check the port is free" in m for m in out)
    assert all(len(m) <= 1200 for m in out)


def test_an_oversized_best_hit_is_truncated_only_when_nothing_else_fits(tmp_path):
    """Whole memories beat a fragment, so truncation is the last resort."""
    c = sqlite3.connect(tmp_path / "only-long.db")
    c.executescript(
        """
        CREATE TABLE session_summaries(id INTEGER PRIMARY KEY, request TEXT, investigated TEXT, learned TEXT);
        CREATE VIRTUAL TABLE session_summaries_fts USING fts5(learned, investigated, request, content='session_summaries', content_rowid='id');
        """
    )
    c.execute("INSERT INTO session_summaries VALUES (1, 'tunnel', 'tunnel', ?)", ("tunnel " + "y" * 3000,))
    c.execute("INSERT INTO session_summaries_fts(rowid, learned, investigated, request) SELECT id, learned, investigated, request FROM session_summaries")
    c.commit(); c.close()
    out = memory.recall("tunnel", k=5, char_budget=1200, cache=tmp_path / "only-long.db")
    assert len(out) == 1 and out[0].endswith("\u2026")
    assert len(out[0]) <= 1201, "the fragment must respect the budget"


def test_a_budget_too_small_to_say_anything_returns_nothing(tmp_path):
    """Truncating to a handful of characters is noise, not memory."""
    db = _store_with_a_long_memory(tmp_path / "claude-mem.db")
    assert memory.recall("tunnel", k=5, char_budget=10, cache=db) == []


def test_recall_fails_open_on_missing_or_corrupt_store(tmp_path, caplog):
    assert memory.recall("anything", cache=tmp_path / "missing.db") == []
    bad = tmp_path / "bad.db"; bad.write_bytes(b"not a database")
    with caplog.at_level(logging.WARNING):
        assert memory.recall("anything", cache=bad) == []


@pytest.mark.asyncio
async def test_codex_recall_uses_settings_budget_and_never_raises(tmp_path):
    db = _store(tmp_path / "claude-mem.db")
    settings = Settings(_env_file=None, codex_memory_top_k=3, codex_memory_char_budget=400)
    assert "check the port is free" in (await recall_context("port tunnel", settings, db=db))[0]
    assert await recall_context("port tunnel", settings, db=tmp_path / "nope.db") == []
