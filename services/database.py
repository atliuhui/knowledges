"""SQLite store for tool-maintained runtime metadata (convert / index state)."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id                      TEXT PRIMARY KEY,
    source_path             TEXT NOT NULL,
    source_hash             TEXT,
    processed_path          TEXT,
    processed_hash          TEXT,

    converter_version       TEXT,
    convert_fingerprint     TEXT,
    convert_status          TEXT,         -- pending / ok / failed / skipped
    convert_error           TEXT,
    converted_at            TEXT,

    chunking_version        TEXT,

    fulltext_engine         TEXT,
    fulltext_index_version  TEXT,
    fulltext_index_status   TEXT,         -- pending / ok / failed / skipped
    fulltext_index_error    TEXT,
    fulltext_indexed_at     TEXT,

    vector_engine           TEXT,
    embedding_model         TEXT,
    vector_index_version    TEXT,
    vector_index_status     TEXT,         -- pending / ok / failed / skipped
    vector_index_error      TEXT,
    vector_indexed_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_documents_source_path ON documents(source_path);
CREATE INDEX IF NOT EXISTS idx_documents_convert_status ON documents(convert_status);
"""


@dataclass
class DocRecord:
    id: str
    source_path: str
    source_hash: str | None = None
    processed_path: str | None = None
    processed_hash: str | None = None
    converter_version: str | None = None
    convert_fingerprint: str | None = None
    convert_status: str | None = None
    convert_error: str | None = None
    converted_at: str | None = None
    chunking_version: str | None = None
    fulltext_engine: str | None = None
    fulltext_index_version: str | None = None
    fulltext_index_status: str | None = None
    fulltext_index_error: str | None = None
    fulltext_indexed_at: str | None = None
    vector_engine: str | None = None
    embedding_model: str | None = None
    vector_index_version: str | None = None
    vector_index_status: str | None = None
    vector_index_error: str | None = None
    vector_indexed_at: str | None = None


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get(self, doc_id: str) -> DocRecord | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM documents WHERE id = ?", (doc_id,)
            ).fetchone()
        return _row_to_record(row) if row else None

    def upsert(self, rec: DocRecord) -> None:
        """Write every column of ``rec`` (including ``None``) to the row.

        Use this only when you intend to set the full record. For step-local
        partial writes (convert touching convert_* columns, ingest touching
        index_* columns), use :meth:`update` instead so that unrelated columns
        are preserved.
        """
        fields = list(rec.__dict__.keys())
        placeholders = ",".join("?" for _ in fields)
        cols = ",".join(fields)
        updates = ",".join(f"{c}=excluded.{c}" for c in fields if c != "id")
        sql = (
            f"INSERT INTO documents ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}"
        )
        with self._conn() as conn:
            conn.execute(sql, tuple(getattr(rec, f) for f in fields))

    def update(self, doc_id: str, source_path: str, **fields: object) -> None:
        """Partial upsert: only the explicitly supplied columns are written.

        Any column not listed in ``fields`` keeps its existing value. ``None``
        values ARE written (use this to clear an error column, etc.). ``id`` is
        positional; ``source_path`` is required so we can satisfy the NOT NULL
        constraint when the row is being inserted for the first time.
        """
        all_fields = {"source_path": source_path, **fields}
        cols = ",".join(all_fields.keys())
        placeholders = ",".join("?" for _ in all_fields)
        updates = ",".join(f"{c}=excluded.{c}" for c in all_fields.keys())
        sql = (
            f"INSERT INTO documents (id, {cols}) VALUES (?, {placeholders}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}"
        )
        with self._conn() as conn:
            conn.execute(sql, (doc_id, *all_fields.values()))

    def all(self) -> list[DocRecord]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM documents").fetchall()
        return [_row_to_record(r) for r in rows]

    def needing_index(self) -> list[DocRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM documents WHERE convert_status = 'ok' "
                "AND (fulltext_index_status IS NULL OR fulltext_index_status != 'ok' "
                "     OR vector_index_status IS NULL OR vector_index_status != 'ok')"
            ).fetchall()
        return [_row_to_record(r) for r in rows]


def _row_to_record(row: sqlite3.Row) -> DocRecord:
    return DocRecord(**{k: row[k] for k in row.keys()})
