"""Private SQLite storage for exact, lineage-scoped local context history."""

from __future__ import annotations

from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from .context_segments import ContextSegment, NormalizedContext, canonical_json


class ContextStoreUnavailable(RuntimeError):
    """The private archive could not safely complete the requested operation."""


@dataclass(frozen=True)
class StoredLineage:
    lineage_id: str
    parent_id: str | None
    matched_prefix: int
    segment_ids: tuple[str, ...]


@dataclass(frozen=True)
class StoredSegment:
    """One ordered lineage occurrence backed by a content-addressed body."""

    segment_id: str
    ordinal: int
    role: str
    kind: str
    exact_json: str
    searchable_text: str
    pair_id: str | None


_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


class ContextStore:
    """Keep exact request bodies local with content deduplication and branch isolation."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int = 1_073_741_824,
        inactive_days: int = 30,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path).expanduser()
        self.max_bytes = max_bytes
        self.inactive_days = inactive_days
        self._clock = clock
        self._lock = threading.RLock()
        try:
            self._secure_directory()
            with self._connect() as connection:
                self._create_schema(connection)
        except (OSError, sqlite3.Error) as exc:
            raise ContextStoreUnavailable("private transcript archive is unavailable") from exc
        self._secure_files()

    def archive(self, context: NormalizedContext) -> StoredLineage:
        """Archive one exact normalized request and return its immutable lineage snapshot."""
        segment_ids = tuple(segment.segment_id for segment in context.segments)
        fingerprint = self._fingerprint(context)
        now = self._clock()
        try:
            with self._lock, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                exact = connection.execute(
                    """
                    SELECT lineage_id, parent_id
                    FROM lineages
                    WHERE client_kind = ? AND fingerprint = ?
                    """,
                    (context.client_kind, fingerprint),
                ).fetchone()
                if exact is not None:
                    connection.execute(
                        "UPDATE lineages SET last_active_at = ? WHERE lineage_id = ?",
                        (now, exact["lineage_id"]),
                    )
                    connection.commit()
                    return StoredLineage(
                        lineage_id=exact["lineage_id"],
                        parent_id=exact["parent_id"],
                        matched_prefix=len(context.segments),
                        segment_ids=segment_ids,
                    )

                parent_id, matched_prefix = self._parent_for(connection, context)
                lineage_id = uuid4().hex
                connection.execute(
                    """
                    INSERT INTO lineages(
                        lineage_id, client_kind, fingerprint, parent_id, created_at, last_active_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        lineage_id,
                        context.client_kind,
                        fingerprint,
                        parent_id,
                        now,
                        now,
                    ),
                )
                for segment in context.segments:
                    self._store_segment(connection, segment)
                    connection.execute(
                        """
                        INSERT INTO lineage_segments(lineage_id, segment_id, ordinal, content_hash)
                        VALUES (?, ?, ?, ?)
                        """,
                        (lineage_id, segment.segment_id, segment.ordinal, segment.content_hash),
                    )
                connection.commit()
                return StoredLineage(lineage_id, parent_id, matched_prefix, segment_ids)
        except (OSError, sqlite3.Error) as exc:
            raise ContextStoreUnavailable("private transcript archive is unavailable") from exc
        finally:
            self._secure_files()

    def segments(self, lineage_id: str) -> list[StoredSegment]:
        """Return exact segment occurrences in their original request order."""
        try:
            with self._lock, self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT ls.segment_id, ls.ordinal, s.role, s.kind, s.exact_json,
                           s.searchable_text, s.pair_id
                    FROM lineage_segments AS ls
                    JOIN segments AS s ON s.content_hash = ls.content_hash
                    WHERE ls.lineage_id = ?
                    ORDER BY ls.ordinal
                    """,
                    (lineage_id,),
                ).fetchall()
                return [self._stored_segment(row) for row in rows]
        except (OSError, sqlite3.Error) as exc:
            raise ContextStoreUnavailable("private transcript archive is unavailable") from exc

    def search(
        self,
        lineage_id: str,
        query: str,
        limit: int,
        exclude_ids: Collection[str],
    ) -> list[StoredSegment]:
        """Search only one lineage, using literal terms rather than FTS operators."""
        terms = _TOKEN.findall(query)
        if not terms or limit <= 0:
            return []
        fts_query = " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
        excluded = tuple(exclude_ids)
        exclusions = ""
        parameters: list[Any] = [fts_query, lineage_id]
        if excluded:
            exclusions = f" AND ls.segment_id NOT IN ({','.join('?' for _ in excluded)})"
            parameters.extend(excluded)
        parameters.append(limit)
        try:
            with self._lock, self._connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT ls.segment_id, ls.ordinal, s.role, s.kind, s.exact_json,
                           s.searchable_text, s.pair_id
                    FROM segments_fts AS fts
                    JOIN lineage_segments AS ls ON ls.content_hash = fts.content_hash
                    JOIN segments AS s ON s.content_hash = ls.content_hash
                    WHERE fts.searchable_text MATCH ? AND ls.lineage_id = ?{exclusions}
                    ORDER BY bm25(segments_fts), ls.ordinal
                    LIMIT ?
                    """,
                    parameters,
                ).fetchall()
                return [self._stored_segment(row) for row in rows]
        except (OSError, sqlite3.Error) as exc:
            raise ContextStoreUnavailable("private transcript archive is unavailable") from exc

    def get_cached_response(self, key: str, now: float) -> dict[str, Any] | None:
        """Return a completed response only while its caller-provided TTL remains valid."""
        try:
            with self._lock, self._connect() as connection:
                row = connection.execute(
                    "SELECT response_json, expires_at FROM responses WHERE cache_key = ?", (key,)
                ).fetchone()
                if row is None:
                    return None
                if row["expires_at"] <= now:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("DELETE FROM responses WHERE cache_key = ?", (key,))
                    connection.commit()
                    return None
                response = json.loads(row["response_json"])
                return response if isinstance(response, dict) else None
        except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
            raise ContextStoreUnavailable("private transcript archive is unavailable") from exc
        finally:
            self._secure_files()

    def put_cached_response(self, key: str, response: dict[str, Any], expires_at: float) -> None:
        """Store a complete response for retry coalescing; partial streams are never accepted."""
        try:
            response_json = canonical_json(response)
            with self._lock, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO responses(cache_key, response_json, expires_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        response_json = excluded.response_json,
                        expires_at = excluded.expires_at
                    """,
                    (key, response_json, expires_at),
                )
                connection.commit()
        except (OSError, sqlite3.Error) as exc:
            raise ContextStoreUnavailable("private transcript archive is unavailable") from exc
        finally:
            self._secure_files()

    def database_size(self) -> int:
        """Return the combined SQLite database, WAL, and shared-memory footprint."""
        return sum(
            candidate.stat().st_size
            for candidate in self._database_files()
            if candidate.exists()
        )

    def prune_if_needed(self, now: float) -> int:
        """Drop inactive lineage snapshots only after the archive crosses its soft cap."""
        if self.database_size() <= self.max_bytes:
            return 0
        cutoff = now - self.inactive_days * 86_400
        try:
            with self._lock, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                stale = connection.execute(
                    """
                    SELECT lineage_id FROM lineages
                    WHERE last_active_at < ?
                    ORDER BY last_active_at, created_at, lineage_id
                    """,
                    (cutoff,),
                ).fetchall()
                stale_ids = [row["lineage_id"] for row in stale]
                if not stale_ids:
                    connection.commit()
                    return 0
                placeholders = ",".join("?" for _ in stale_ids)
                connection.execute(
                    f"DELETE FROM lineages WHERE lineage_id IN ({placeholders})", stale_ids
                )
                orphaned = connection.execute(
                    """
                    SELECT content_hash FROM segments
                    WHERE content_hash NOT IN (SELECT content_hash FROM lineage_segments)
                    """
                ).fetchall()
                for row in orphaned:
                    connection.execute(
                        "DELETE FROM segments_fts WHERE content_hash = ?", (row["content_hash"],)
                    )
                connection.execute(
                    """
                    DELETE FROM segments
                    WHERE content_hash NOT IN (SELECT content_hash FROM lineage_segments)
                    """
                )
                connection.commit()
                return len(stale_ids)
        except (OSError, sqlite3.Error) as exc:
            raise ContextStoreUnavailable("private transcript archive is unavailable") from exc
        finally:
            self._secure_files()

    def journal_mode(self) -> str:
        try:
            with self._lock, self._connect() as connection:
                row = connection.execute("PRAGMA journal_mode").fetchone()
                return str(row[0]).lower()
        except (OSError, sqlite3.Error) as exc:
            raise ContextStoreUnavailable("private transcript archive is unavailable") from exc

    def _parent_for(
        self, connection: sqlite3.Connection, context: NormalizedContext
    ) -> tuple[str | None, int]:
        incoming = tuple(segment.content_hash for segment in context.segments)
        candidates: list[tuple[str, int]] = []
        lineage_rows = connection.execute(
            "SELECT lineage_id FROM lineages WHERE client_kind = ?", (context.client_kind,)
        ).fetchall()
        for row in lineage_rows:
            stored = tuple(
                member["content_hash"]
                for member in connection.execute(
                    """
                    SELECT content_hash FROM lineage_segments
                    WHERE lineage_id = ?
                    ORDER BY ordinal
                    """,
                    (row["lineage_id"],),
                )
            )
            prefix = self._matching_prefix(incoming, stored)
            if prefix:
                candidates.append((row["lineage_id"], prefix))
        if not candidates:
            return None, 0
        longest = max(prefix for _, prefix in candidates)
        best = [lineage_id for lineage_id, prefix in candidates if prefix == longest]
        if len(best) != 1:
            return None, 0
        return best[0], longest

    @staticmethod
    def _matching_prefix(left: tuple[str, ...], right: tuple[str, ...]) -> int:
        length = 0
        for incoming, stored in zip(left, right, strict=False):
            if incoming != stored:
                break
            length += 1
        return length

    @staticmethod
    def _fingerprint(context: NormalizedContext) -> str:
        material = canonical_json(
            {
                "client_kind": context.client_kind,
                "content_hashes": [segment.content_hash for segment in context.segments],
            }
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _stored_segment(row: sqlite3.Row) -> StoredSegment:
        return StoredSegment(
            segment_id=row["segment_id"],
            ordinal=row["ordinal"],
            role=row["role"],
            kind=row["kind"],
            exact_json=row["exact_json"],
            searchable_text=row["searchable_text"],
            pair_id=row["pair_id"],
        )

    @staticmethod
    def _store_segment(connection: sqlite3.Connection, segment: ContextSegment) -> None:
        inserted = connection.execute(
            """
            INSERT INTO segments(content_hash, role, kind, exact_json, searchable_text, pair_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(content_hash) DO NOTHING
            """,
            (
                segment.content_hash,
                segment.role,
                segment.kind,
                segment.exact_json,
                segment.searchable_text,
                segment.pair_id,
            ),
        ).rowcount
        if inserted:
            connection.execute(
                "INSERT INTO segments_fts(content_hash, searchable_text) VALUES (?, ?)",
                (segment.content_hash, segment.searchable_text),
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(str(self.path), timeout=0.5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout = 500")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS lineages (
                lineage_id TEXT PRIMARY KEY,
                client_kind TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                parent_id TEXT REFERENCES lineages(lineage_id) ON DELETE SET NULL,
                created_at REAL NOT NULL,
                last_active_at REAL NOT NULL,
                UNIQUE(client_kind, fingerprint)
            );
            CREATE INDEX IF NOT EXISTS lineages_client_kind ON lineages(client_kind);
            CREATE INDEX IF NOT EXISTS lineages_last_active ON lineages(last_active_at);

            CREATE TABLE IF NOT EXISTS segments (
                content_hash TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                kind TEXT NOT NULL,
                exact_json TEXT NOT NULL,
                searchable_text TEXT NOT NULL,
                pair_id TEXT
            );

            CREATE TABLE IF NOT EXISTS lineage_segments (
                lineage_id TEXT NOT NULL REFERENCES lineages(lineage_id) ON DELETE CASCADE,
                segment_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                content_hash TEXT NOT NULL REFERENCES segments(content_hash),
                PRIMARY KEY(lineage_id, ordinal),
                UNIQUE(lineage_id, segment_id)
            );
            CREATE INDEX IF NOT EXISTS lineage_segments_content_hash
                ON lineage_segments(content_hash);

            CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
                content_hash UNINDEXED,
                searchable_text
            );

            CREATE TABLE IF NOT EXISTS responses (
                cache_key TEXT PRIMARY KEY,
                response_json TEXT NOT NULL,
                expires_at REAL NOT NULL
            );
            """
        )
        connection.commit()

    def _secure_directory(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)

    def _database_files(self) -> tuple[Path, Path, Path]:
        return (
            self.path,
            self.path.with_name(f"{self.path.name}-wal"),
            self.path.with_name(f"{self.path.name}-shm"),
        )

    def _secure_files(self) -> None:
        self._secure_directory()
        for candidate in self._database_files():
            if candidate.exists():
                os.chmod(candidate, 0o600)
