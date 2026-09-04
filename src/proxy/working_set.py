"""A bounded, PREFIX-STABLE working set for local-tier requests.

Why this exists, in one measurement. On 2026-09-05, against
`qwen3.8:27b-obliterated` on this host:

    cold prefill  6,840 tokens   26.3 s   (260 tok/s)
    cold prefill 12,815 tokens   49.1 s   (261 tok/s)
    cold prefill 27,008 tokens  135.6 s   (199 tok/s)
    append ~800 tokens to a transcript the model just saw   5-10 s
    byte-identical repeat                                    0.7 s

The expensive event is not the size of the conversation. It is presenting the
model with a prompt whose PREFIX it has not already processed: Ollama reuses
the KV cache for a shared prefix, so an ordinary turn costs seconds while the
same transcript presented cold costs minutes.

That is the whole design constraint here, and it is why this module exists
rather than a summarizer. Two consequences:

1. Trimming to fit is cheaper than escalating. The 256K 4B tier prefills
   103,277 tokens in 391 s and 142K in roughly nine minutes — the 2026-09-04
   session that showed 100% context and looked frozen. Trimming the same
   session to 18K and keeping the 27B costs about 70 s, once.

2. A window recomputed on EVERY request would be worse than no window at all.
   Shifting the boundary each turn changes the prefix each turn, which turns a
   5-10 s append into a 40-50 s cold prefill. So the boundary is sticky: it is
   chosen when the ceiling is crossed and then REUSED, unchanged, for as long
   as the conversation keeps fitting under it. One turn per cycle pays the
   rebuild; the rest append.

Model-written compaction was rejected on the same evidence. The 27B decodes at
8.9 tok/s, so a 1,500-token summary costs 169 s and a 2,500-token one 281 s,
plus a cold prefill of whatever it produced — 3.5 to 5.5 minutes per
compaction, every 6-15 turns. Selection costs no model call at all.

The client remains the source of truth. It resends its full transcript every
turn; this module only decides how much of that is forwarded to a local tier,
exactly as bare-mode decides how much of each message is forwarded. Nothing is
deleted anywhere.
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Iterable

from .tokens import count_messages

logger = logging.getLogger(__name__)

# Lineage → the index its window currently starts at. Bounded so a long-lived
# router cannot accumulate one entry per session it has ever seen; the loser of
# an eviction pays one rebuild, which is the same cost as a first request.
_MAX_TRACKED = 128
_boundaries: "OrderedDict[str, int]" = OrderedDict()


@dataclass(frozen=True)
class Bounded:
    """The messages to send, and why they were chosen."""

    messages: list[Any]
    keep_from: int
    tokens: int
    rebuilt: bool
    overflow: bool  # even the tail alone exceeds the ceiling — caller escalates


def reset() -> None:
    """Forget every boundary. Tests only."""
    _boundaries.clear()


def lineage_key(messages: list[Any], profile: str) -> str:
    """Identify the conversation, not the request.

    The head of a transcript is stable for its whole life — the client resends
    it verbatim every turn — so hashing the first message identifies the
    session across turns without the client having to tell us anything. The
    profile joins the key because two tiers have different ceilings and must
    not share a boundary.
    """
    head = messages[0] if messages else None
    role = getattr(head, "role", "") or ""
    content = getattr(head, "content", "") or ""
    digest = hashlib.sha256(f"{role}\x00{content!r}".encode()).hexdigest()[:32]
    return f"{profile}:{digest}"


def _blocks(msg: Any) -> list[dict]:
    content = getattr(msg, "content", None)
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _first_safe_start(messages: list[Any], start: int) -> int:
    """Advance `start` past any message that would open with an orphan result.

    A `tool_result` block carries only a `tool_use_id`. Kept tools keep that
    structure (see bare.strip_message), so a window that begins after the
    matching `tool_use` would send the backend a result for a call it cannot
    see. Anthropic rejects that shape, and it is nonsense to a local model.
    """
    while start < len(messages) - 1:
        produced = {
            str(b.get("id"))
            for m in messages[start:]
            for b in _blocks(m)
            if b.get("type") == "tool_use" and b.get("id")
        }
        orphan = any(
            b.get("type") == "tool_result"
            and str(b.get("tool_use_id", "")) not in produced
            for b in _blocks(messages[start])
        )
        if not orphan:
            return start
        start += 1
    return start


def bound(
    messages: list[Any],
    system: Any = None,
    tools: Iterable[Any] | None = None,
    *,
    profile: str,
    target: int,
    ceiling: int,
) -> Bounded:
    """Choose a bounded, prefix-stable suffix of `messages`.

    `target` is what a rebuild aims for; `ceiling` is what triggers one. The gap
    between them is the headroom a session spends before paying another
    rebuild, so a target far below the ceiling buys fewer rebuilds at the cost
    of less history.
    """
    tool_list = list(tools) if tools else None
    total = count_messages(messages, system, tool_list)
    if total <= ceiling:
        # Fits whole: the natural boundary is the start, and remembering it
        # keeps a later rebuild from being treated as the first one.
        _remember(lineage_key(messages, profile), 0)
        return Bounded(messages, 0, total, rebuilt=False, overflow=False)

    key = lineage_key(messages, profile)
    remembered = _boundaries.get(key)
    if remembered is not None and 0 < remembered < len(messages):
        kept = messages[remembered:]
        held = count_messages(kept, system, tool_list)
        if held <= ceiling:
            # The sticky path, and the common one: the boundary that was
            # chosen at the last rebuild still fits, so the prefix the model
            # already has stays byte-identical and this turn is an append.
            _remember(key, remembered)
            return Bounded(kept, remembered, held, rebuilt=False, overflow=False)

    start = _rebuild_start(messages, system, tool_list, target)
    start = _first_safe_start(messages, start)
    kept = messages[start:]
    held = count_messages(kept, system, tool_list)
    _remember(key, start)
    return Bounded(kept, start, held, rebuilt=True, overflow=held > ceiling)


def _rebuild_start(
    messages: list[Any], system: Any, tools: list[Any] | None, target: int
) -> int:
    """Walk back from the newest message until `target` would be exceeded.

    Newest-first because the current instruction and the live tool loop are the
    part a local model cannot work without. Always keeps the last message even
    when it alone is over budget — the caller decides what to do about that,
    and a request with no instruction is never the better answer.
    """
    start = len(messages) - 1
    while start > 0:
        candidate = count_messages(messages[start - 1:], system, tools)
        if candidate > target:
            break
        start -= 1
    return start


def _remember(key: str, start: int) -> None:
    _boundaries[key] = start
    _boundaries.move_to_end(key)
    while len(_boundaries) > _MAX_TRACKED:
        _boundaries.popitem(last=False)
