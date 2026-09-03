"""Client-neutral, exact request-body segments for context compaction."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
import hashlib
import json
from typing import Any, Literal, Protocol


ClientKind = Literal["claude", "codex"]


def canonical_json(value: Any) -> str:
    """Render request values deterministically without losing Unicode text."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def segment_identifier(
    client_kind: ClientKind, role: str, kind: str, native_content: Any
) -> str:
    """Return the content-addressed identity shared by equal native segments."""
    material = canonical_json(
        {
            "client_kind": client_kind,
            "role": role,
            "kind": kind,
            "native_content": native_content,
        }
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ContextSegment:
    segment_id: str
    ordinal: int
    role: str
    kind: str
    exact_json: str
    searchable_text: str
    pair_id: str | None = None


@dataclass(frozen=True)
class NormalizedContext:
    client_kind: ClientKind
    model: str
    segments: tuple[ContextSegment, ...]
    current_segment_id: str
    native: Any


class ContextAdapter(Protocol):
    def normalize(self, payload: Any) -> NormalizedContext: ...

    def rebuild(
        self,
        context: NormalizedContext,
        selected_ids: Collection[str],
        historical_text: str | None = None,
    ) -> Any: ...


def selected_with_pairs(
    segments: Collection[ContextSegment], selected_ids: Collection[str]
) -> set[str]:
    """Close selected ids over tool call/result pair membership."""
    selected = set(selected_ids)
    selected_pairs = {
        segment.pair_id
        for segment in segments
        if segment.segment_id in selected and segment.pair_id is not None
    }
    selected.update(
        segment.segment_id
        for segment in segments
        if segment.pair_id is not None and segment.pair_id in selected_pairs
    )
    return selected
