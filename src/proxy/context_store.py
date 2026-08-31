"""Exact, lineage-scoped transcript storage for offline failover."""

from __future__ import annotations

from collections.abc import Collection, Callable
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
import uuid
from typing import Any

from .models import MessagesRequest


_FTS_TERMS = re.compile(r"[\w./:-]+", re.UNICODE)


@dataclass(frozen=True)
class StoredLineage:
    lineage_id: str
    parent_id: str | None
    matched_prefix: int
    segment_hashes: tuple[str, ...]


@dataclass(frozen=True)
class StoredSegment:
    segment_hash: str
    ordinal: int
    role: str
    exact_json: str
    searchable_text: str


class ContextStoreUnavailable(RuntimeError):
    """The local archive cannot accept a request without risking routing."""


class ContextStore:
    def __init__(
        self,
        path: str | Path,
        *,
        max_bytes: int = 1_073_741_824,
        inactive_days: int = 30,
        now_fn: Callable[[], float] = time.time,
        busy_timeout_ms: int = 500,
    ) -> None:
        self.path = Path(path).expanduser()
        self.max_bytes = max_bytes
        self.inactive_days = inactive_days
        self._now = now_fn
        self._busy_timeout_ms = busy_timeout_ms
        self._lock = threading.RLock()
        self.writable = True
        self._prepare_path()
        self._initialize()

    def _prepare_path(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self._busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        return connection

    def _initialize(self) -> None:
        schema = """
        CREATE TABLE IF NOT EXISTS lineages (
          lineage_id TEXT PRIMARY KEY,
          parent_id TEXT REFERENCES lineages(lineage_id) ON DELETE SET NULL,
          client_kind TEXT NOT NULL,
          created_at REAL NOT NULL,
          last_seen_at REAL NOT NULL,
          current_head_hash TEXT
        );
        CREATE TABLE IF NOT EXISTS segments (
          segment_hash TEXT PRIMARY KEY,
          role TEXT NOT NULL,
          exact_json TEXT NOT NULL,
          searchable_text TEXT NOT NULL,
          created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lineage_segments (
          lineage_id TEXT NOT NULL REFERENCES lineages(lineage_id) ON DELETE CASCADE,
          ordinal INTEGER NOT NULL,
          segment_hash TEXT NOT NULL REFERENCES segments(segment_hash),
          PRIMARY KEY (lineage_id, ordinal)
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts
          USING fts5(segment_hash UNINDEXED, searchable_text);
        CREATE TABLE IF NOT EXISTS responses (
          request_hash TEXT PRIMARY KEY,
          response_json TEXT NOT NULL,
          expires_at REAL NOT NULL,
          created_at REAL NOT NULL
        );
        """
        try:
            with self._lock:
                connection = self._connect()
                try:
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.executescript(schema)
                finally:
                    connection.close()
            self._secure_files()
        except sqlite3.Error as exc:
            self.writable = False
            raise ContextStoreUnavailable("context database initialization failed") from exc

    def _secure_files(self) -> None:
        for candidate in (
            self.path,
            self.path.with_name(f"{self.path.name}-wal"),
            self.path.with_name(f"{self.path.name}-shm"),
        ):
            if candidate.exists():
                candidate.chmod(stat.S_IRUSR | stat.S_IWUSR)

    @staticmethod
    def _searchable(content: Any) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return json.dumps(content, ensure_ascii=False, sort_keys=True)
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            block_type = block.get("type")
            if block_type == "text":
                parts.append(str(block.get("text", "")))
            elif block_type == "tool_use":
                parts.append(str(block.get("name", "")))
                parts.append(
                    json.dumps(block.get("input", {}), ensure_ascii=False, sort_keys=True)
                )
            elif block_type == "tool_result":
                parts.append(ContextStore._searchable(block.get("content")))
        return "\n".join(part for part in parts if part)

    @staticmethod
    def _canonical(role: str, content: Any) -> tuple[str, str, str]:
        exact_json = json.dumps(
            {"content": content, "role": role},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        segment_hash = hashlib.sha256(exact_json.encode("utf-8")).hexdigest()
        return segment_hash, exact_json, ContextStore._searchable(content)

    @staticmethod
    def _prefix(left: tuple[str, ...], right: tuple[str, ...]) -> int:
        count = 0
        for old, new in zip(left, right):
            if old != new:
                break
            count += 1
        return count

    def _request_segments(self, req: MessagesRequest) -> list[tuple[str, str, str, str]]:
        items: list[tuple[str, Any]] = []
        if req.system is not None:
            items.append(("system", req.system))
        items.extend((message.role, message.content) for message in req.messages)
        return [
            (segment_hash, role, exact_json, searchable_text)
            for role, content in items
            for segment_hash, exact_json, searchable_text in [self._canonical(role, content)]
        ]

    @staticmethod
    def _lineage_hashes(connection: sqlite3.Connection) -> dict[str, tuple[str, ...]]:
        rows = connection.execute(
            """
            SELECT lineage_id, segment_hash
            FROM lineage_segments
            ORDER BY lineage_id, ordinal
            """
        ).fetchall()
        hashes: dict[str, list[str]] = {}
        for row in rows:
            hashes.setdefault(row["lineage_id"], []).append(row["segment_hash"])
        for row in connection.execute("SELECT lineage_id FROM lineages"):
            hashes.setdefault(row["lineage_id"], [])
        return {key: tuple(value) for key, value in hashes.items()}

    def archive_request(
        self,
        req: MessagesRequest,
        client_kind: str = "claude",
    ) -> StoredLineage:
        if not self.writable:
            raise ContextStoreUnavailable("context archive is disabled")
        segments = self._request_segments(req)
        requested_hashes = tuple(segment[0] for segment in segments)
        now = self._now()

        try:
            with self._lock:
                connection = self._connect()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    known = self._lineage_hashes(connection)
                    prefix_by_id = {
                        lineage_id: self._prefix(hashes, requested_hashes)
                        for lineage_id, hashes in known.items()
                    }
                    best_prefix = max(prefix_by_id.values(), default=0)
                    best_ids = [
                        lineage_id
                        for lineage_id, prefix in prefix_by_id.items()
                        if prefix == best_prefix and prefix > 0
                    ]

                    lineage_id: str
                    parent_id: str | None
                    if len(best_ids) == 1:
                        candidate = best_ids[0]
                        candidate_hashes = known[candidate]
                        extends_candidate = (
                            best_prefix == len(candidate_hashes)
                            and len(requested_hashes) >= len(candidate_hashes)
                        )
                        if extends_candidate:
                            lineage_id = candidate
                            parent_row = connection.execute(
                                "SELECT parent_id FROM lineages WHERE lineage_id=?",
                                (lineage_id,),
                            ).fetchone()
                            parent_id = parent_row["parent_id"] if parent_row else None
                        else:
                            lineage_id = uuid.uuid4().hex
                            parent_id = candidate
                    else:
                        lineage_id = uuid.uuid4().hex
                        parent_id = None

                    existing = known.get(lineage_id, ())
                    if lineage_id not in known:
                        connection.execute(
                            """
                            INSERT INTO lineages(
                              lineage_id, parent_id, client_kind, created_at,
                              last_seen_at, current_head_hash
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                lineage_id,
                                parent_id,
                                client_kind,
                                now,
                                now,
                                requested_hashes[-1] if requested_hashes else None,
                            ),
                        )

                    for ordinal, (segment_hash, role, exact_json, searchable_text) in enumerate(segments):
                        inserted = connection.execute(
                            """
                            INSERT OR IGNORE INTO segments(
                              segment_hash, role, exact_json, searchable_text, created_at
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            (segment_hash, role, exact_json, searchable_text, now),
                        ).rowcount
                        if inserted:
                            connection.execute(
                                "INSERT INTO segments_fts(segment_hash, searchable_text) VALUES (?, ?)",
                                (segment_hash, searchable_text),
                            )
                        if ordinal >= len(existing):
                            connection.execute(
                                """
                                INSERT OR REPLACE INTO lineage_segments(
                                  lineage_id, ordinal, segment_hash
                                ) VALUES (?, ?, ?)
                                """,
                                (lineage_id, ordinal, segment_hash),
                            )

                    connection.execute(
                        """
                        UPDATE lineages
                        SET last_seen_at=?, current_head_hash=?
                        WHERE lineage_id=?
                        """,
                        (
                            now,
                            requested_hashes[-1] if requested_hashes else None,
                            lineage_id,
                        ),
                    )
                    connection.execute("COMMIT")
                except BaseException:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise
                finally:
                    connection.close()
            self._secure_files()
        except sqlite3.Error as exc:
            if "full" in str(exc).lower():
                self.writable = False
            raise ContextStoreUnavailable("context archive write failed") from exc

        return StoredLineage(
            lineage_id=lineage_id,
            parent_id=parent_id,
            matched_prefix=best_prefix,
            segment_hashes=requested_hashes,
        )

    def segments(self, lineage_id: str) -> list[StoredSegment]:
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute(
                    """
                    SELECT s.segment_hash, ls.ordinal, s.role,
                           s.exact_json, s.searchable_text
                    FROM lineage_segments AS ls
                    JOIN segments AS s ON s.segment_hash = ls.segment_hash
                    WHERE ls.lineage_id=?
                    ORDER BY ls.ordinal
                    """,
                    (lineage_id,),
                ).fetchall()
            finally:
                connection.close()
        return [StoredSegment(**dict(row)) for row in rows]

    @staticmethod
    def _fts_query(query: str) -> str:
        terms = []
        seen = set()
        for term in _FTS_TERMS.findall(query):
            normalized = term.casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            terms.append(f'"{term.replace(chr(34), chr(34) * 2)}"')
        return " OR ".join(terms[:32])

    def search(
        self,
        lineage_id: str,
        query: str,
        limit: int = 6,
        exclude_hashes: Collection[str] = (),
    ) -> list[StoredSegment]:
        fts_query = self._fts_query(query)
        if not fts_query or limit <= 0:
            return []
        excluded = set(exclude_hashes)
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute(
                    """
                    SELECT s.segment_hash, ls.ordinal, s.role,
                           s.exact_json, s.searchable_text,
                           bm25(segments_fts) AS rank
                    FROM segments_fts
                    JOIN segments AS s
                      ON s.segment_hash = segments_fts.segment_hash
                    JOIN lineage_segments AS ls
                      ON ls.segment_hash = s.segment_hash
                    WHERE segments_fts MATCH ? AND ls.lineage_id=?
                    ORDER BY rank ASC, ls.ordinal DESC
                    LIMIT ?
                    """,
                    (fts_query, lineage_id, limit + len(excluded)),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
            finally:
                connection.close()
        return [
            StoredSegment(
                segment_hash=row["segment_hash"],
                ordinal=row["ordinal"],
                role=row["role"],
                exact_json=row["exact_json"],
                searchable_text=row["searchable_text"],
            )
            for row in rows
            if row["segment_hash"] not in excluded
        ][:limit]

    def get_cached_response(
        self,
        request_hash: str,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        current = self._now() if now is None else now
        with self._lock:
            connection = self._connect()
            try:
                row = connection.execute(
                    """
                    SELECT response_json FROM responses
                    WHERE request_hash=? AND expires_at>?
                    """,
                    (request_hash, current),
                ).fetchone()
                if row is None:
                    connection.execute(
                        "DELETE FROM responses WHERE request_hash=?",
                        (request_hash,),
                    )
                    return None
                return json.loads(row["response_json"])
            finally:
                connection.close()

    def put_cached_response(
        self,
        request_hash: str,
        response: dict[str, Any],
        expires_at: float,
    ) -> None:
        if not self.writable:
            return
        payload = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            connection = self._connect()
            try:
                connection.execute(
                    """
                    INSERT INTO responses(
                      request_hash, response_json, expires_at, created_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(request_hash) DO UPDATE SET
                      response_json=excluded.response_json,
                      expires_at=excluded.expires_at,
                      created_at=excluded.created_at
                    """,
                    (request_hash, payload, expires_at, self._now()),
                )
            finally:
                connection.close()
        self._secure_files()

    def journal_mode(self) -> str:
        with self._lock:
            connection = self._connect()
            try:
                return str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            finally:
                connection.close()

    def database_size(self) -> int:
        return sum(
            candidate.stat().st_size
            for candidate in (
                self.path,
                self.path.with_name(f"{self.path.name}-wal"),
                self.path.with_name(f"{self.path.name}-shm"),
            )
            if candidate.exists()
        )

    def prune_if_needed(self, now: float | None = None) -> int:
        if self.database_size() <= self.max_bytes:
            return 0
        current = self._now() if now is None else now
        cutoff = current - self.inactive_days * 86_400
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                old_ids = [
                    row["lineage_id"]
                    for row in connection.execute(
                        "SELECT lineage_id FROM lineages WHERE last_seen_at<?",
                        (cutoff,),
                    )
                ]
                for lineage_id in old_ids:
                    connection.execute(
                        "DELETE FROM lineages WHERE lineage_id=?",
                        (lineage_id,),
                    )
                orphaned = [
                    row["segment_hash"]
                    for row in connection.execute(
                        """
                        SELECT s.segment_hash
                        FROM segments AS s
                        LEFT JOIN lineage_segments AS ls
                          ON ls.segment_hash=s.segment_hash
                        WHERE ls.segment_hash IS NULL
                        """
                    )
                ]
                for segment_hash in orphaned:
                    connection.execute(
                        "DELETE FROM segments_fts WHERE segment_hash=?",
                        (segment_hash,),
                    )
                    connection.execute(
                        "DELETE FROM segments WHERE segment_hash=?",
                        (segment_hash,),
                    )
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()
        if self.database_size() > self.max_bytes:
            self.writable = False
        return len(old_ids)
