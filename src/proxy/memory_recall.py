"""Bounded, fail-open recall for Codex requests, from the local claude-mem replica."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from . import memory
from .config import Settings

logger = logging.getLogger(__name__)


async def recall_context(query: str, settings: Settings, db: Path | None = None) -> list[str]:
    """Return relevant memories for `query`, or an empty list on any failure."""
    try:
        return await asyncio.to_thread(
            memory.recall,
            query,
            k=settings.codex_memory_top_k,
            char_budget=settings.codex_memory_char_budget,
            cache=Path(settings.memory_db_path).expanduser() if db is None else db,
        )
    except Exception as exc:  # noqa: BLE001 - memory must fail open
        logger.warning("memory recall failed (%s)", type(exc).__name__)
        return []
