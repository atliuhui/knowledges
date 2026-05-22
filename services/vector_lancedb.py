"""LanceDB vector index wrapper."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    import lancedb  # type: ignore
    import pyarrow as pa  # type: ignore
except ImportError:  # pragma: no cover
    lancedb = None  # type: ignore
    pa = None  # type: ignore


SCHEMA_VERSION = "vector-v1"
TABLE_NAME = "chunks"


@dataclass
class VecHit:
    doc_id: str
    chunk_id: str
    score: float
    text: str
    source_path: str
    processed_path: str
    title: str = ""


def _require() -> None:
    if lancedb is None:
        raise RuntimeError("lancedb / pyarrow are not installed.")


def _arrow_schema(dim: int):  # type: ignore[no-untyped-def]
    _require()
    return pa.schema(
        [
            pa.field("doc_id", pa.string()),
            pa.field("chunk_id", pa.string()),
            pa.field("text", pa.string()),
            pa.field("title", pa.string()),
            pa.field("tags", pa.list_(pa.string())),
            pa.field("type", pa.string()),
            pa.field("confidentiality", pa.string()),
            pa.field("source_path", pa.string()),
            pa.field("processed_path", pa.string()),
            pa.field("processed_hash", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), dim)),
        ]
    )


class VectorIndex:
    def __init__(self, path: Path, dim: int):
        _require()
        path.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.dim = dim
        self.db = lancedb.connect(str(path))
        if TABLE_NAME in self.db.table_names():
            self.table = self.db.open_table(TABLE_NAME)
        else:
            self.table = self.db.create_table(TABLE_NAME, schema=_arrow_schema(dim))

    def delete_doc(self, doc_id: str) -> None:
        self.table.delete(f"doc_id = '{doc_id}'")

    def add_chunks(self, doc_id: str, rows: Iterable[dict[str, Any]]) -> None:
        self.delete_doc(doc_id)
        materialized = list(rows)
        if not materialized:
            return
        self.table.add(materialized)

    def search(self, embedding: list[float], limit: int = 10) -> list[VecHit]:
        rs = self.table.search(embedding).limit(limit).to_list()
        hits: list[VecHit] = []
        for r in rs:
            # LanceDB returns distance in _distance; lower is closer.
            distance = float(r.get("_distance", 0.0))
            score = 1.0 / (1.0 + distance)
            hits.append(
                VecHit(
                    doc_id=r.get("doc_id", ""),
                    chunk_id=r.get("chunk_id", ""),
                    score=score,
                    text=r.get("text", ""),
                    source_path=r.get("source_path", ""),
                    processed_path=r.get("processed_path", ""),
                    title=r.get("title", ""),
                )
            )
        return hits
