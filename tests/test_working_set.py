"""The bounded, prefix-stable local working set.

Every number quoted here was measured on this host on 2026-09-05 against
`qwen3.8:27b-obliterated` (32K window) and `qwen3.5:4b-256k`. They are what
decide the shape of this module, so they belong next to the assertions:

    cold prefill  6,840 tok    26.3 s      cold prefill 27,008 tok   135.6 s
    cold prefill 12,815 tok    49.1 s      append to a live prefix    5-10 s
    identical repeat            0.7 s      decode                   8.9 tok/s
    4B-256k cold 103,277 tok  391.2 s      4B-256k append           1.6-2.4 s
"""

import pytest

from src.proxy import working_set
from src.proxy.models import Message

TARGET = 1_000
CEILING = 1_200


@pytest.fixture(autouse=True)
def _clean():
    working_set.reset()
    yield
    working_set.reset()


def _turns(n, words=60):
    """A transcript of `n` alternating turns, each a few hundred tokens."""
    body = "investigate the directory and report what changed " * words
    return [
        Message(role="user" if i % 2 == 0 else "assistant", content=f"[turn {i}] {body}")
        for i in range(n)
    ]


def _bound(messages):
    return working_set.bound(
        messages, "sys", None, profile="local-qwen38-obliterated",
        target=TARGET, ceiling=CEILING,
    )


def test_a_conversation_under_the_ceiling_is_untouched():
    msgs = _turns(1)
    out = _bound(msgs)

    assert out.keep_from == 0 and out.messages == msgs
    assert not out.rebuilt and not out.overflow


def test_an_oversized_conversation_is_trimmed_under_the_ceiling():
    out = _bound(_turns(20))

    assert out.rebuilt and out.keep_from > 0
    assert out.tokens <= CEILING, f"trimmed to {out.tokens}, over the {CEILING} ceiling"


def test_the_boundary_is_sticky_so_the_next_turn_is_an_append():
    """The property the whole module exists for.

    A window recomputed every request moves the prefix every request, and a
    moved prefix is a cold prefill: 26-136 s instead of the 5-10 s an append
    costs. So a boundary that still fits must be REUSED, not recomputed.
    """
    msgs = _turns(20)
    first = _bound(msgs)
    assert first.rebuilt

    grown = msgs + [Message(role="user", content="and now the next question")]
    second = _bound(grown)

    assert second.keep_from == first.keep_from, (
        f"boundary moved {first.keep_from} -> {second.keep_from} without the "
        "ceiling being crossed; every turn would be a cold prefill"
    )
    assert not second.rebuilt
    assert second.messages[:-1] == first.messages, "the retained prefix changed"


def test_the_boundary_moves_again_only_when_the_ceiling_is_crossed():
    msgs = _turns(20)
    first = _bound(msgs)

    # Grow until the kept window no longer fits.
    grown = msgs + _turns(8)
    later = _bound(grown)

    assert later.rebuilt, "the window never rebuilt despite crossing the ceiling"
    assert later.keep_from > first.keep_from
    assert later.tokens <= CEILING


def test_a_window_never_opens_on_an_orphan_tool_result():
    """A tool_result carries only an id. Starting after its tool_use sends the
    backend a result for a call it cannot see, which Anthropic rejects outright.
    """
    filler = "log line that goes on for a while " * 60
    msgs = (
        _turns(12)
        + [
            Message(role="assistant", content=[
                {"type": "tool_use", "id": "call_1", "name": "Bash", "input": {}}]),
            Message(role="user", content=[
                {"type": "tool_result", "tool_use_id": "call_1", "content": filler}]),
        ]
        + _turns(2)
    )

    out = _bound(msgs)

    head = out.messages[0]
    produced = {
        b.get("id")
        for m in out.messages
        if isinstance(m.content, list)
        for b in m.content
        if isinstance(b, dict) and b.get("type") == "tool_use"
    }
    if isinstance(head.content, list):
        for blk in head.content:
            if isinstance(blk, dict) and blk.get("type") == "tool_result":
                assert blk.get("tool_use_id") in produced, "window opened on an orphan result"


def test_a_single_turn_over_the_ceiling_reports_overflow():
    """Nothing to drop: the caller escalates to the wide tier instead."""
    huge = Message(role="user", content="explain this build log " * 4_000)
    out = _bound([Message(role="user", content="hi"), huge])

    assert out.overflow, "an un-trimmable request must be handed to the ladder"
    assert out.messages[-1] == huge, "the current instruction was dropped"


def test_two_conversations_do_not_share_a_boundary():
    a = _turns(20)
    b = [Message(role="user", content="a different session entirely")] + _turns(19)
    first = _bound(a)
    _bound(b)
    again = _bound(a + [Message(role="user", content="next")])

    assert again.keep_from == first.keep_from, "another session moved this one's window"


def test_tracking_is_bounded():
    """A long-lived router must not accumulate one entry per session forever."""
    for i in range(working_set._MAX_TRACKED + 20):
        working_set.bound(
            [Message(role="user", content=f"session {i}")], None, None,
            profile="p", target=TARGET, ceiling=CEILING,
        )
    assert len(working_set._boundaries) <= working_set._MAX_TRACKED
