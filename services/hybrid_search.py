"""Hybrid search combining Tantivy full-text and LanceDB vector results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import Config


@dataclass
class SearchHit:
    doc_id: str
    chunk_id: str
    score: float
    snippet: str
    source_path: str
    processed_path: str
    title: str = ""
    via: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "chunk_id": self.chunk_id,
            "score": round(self.score, 6),
            "snippet": self.snippet,
            "source_path": self.source_path,
            "processed_path": self.processed_path,
            "title": self.title,
            "via": self.via,
        }


def _embed(cfg: Config, text: str) -> list[float]:
    """Embed a query using the configured local Ollama model. Lazy import.

    The query-side instruction template for Qwen3-Embedding is baked into the
    Ollama model file, so we just call `embed_query`. The embedder instance is
    cached on the module for repeated queries.
    """
    global _EMBEDDER  # noqa: PLW0603
    try:
        embedder = _EMBEDDER  # type: ignore[name-defined]
    except NameError:
        from .embeddings import OllamaEmbedder
        embedder = OllamaEmbedder(cfg.embedding)
        _EMBEDDER = embedder  # type: ignore[name-defined]
    return embedder.embed_query(text)


def search(
    cfg: Config,
    query: str,
    mode: str = "hybrid",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Run a search. `mode` is one of: fulltext, vector, hybrid."""
    mode = mode.lower()
    ft_hits: list[Any] = []
    vec_hits: list[Any] = []

    if mode in {"fulltext", "hybrid"}:
        from .fulltext_tantivy import FullTextIndex
        try:
            ft = FullTextIndex(cfg.fulltext_index_dir)
            ft_hits = ft.search(query, limit=limit)
        except Exception as e:  # noqa: BLE001
            ft_hits = []
            if mode == "fulltext":
                raise RuntimeError(f"fulltext search failed: {e}") from e

    if mode in {"vector", "hybrid"}:
        from .vector_lancedb import VectorIndex
        try:
            vec = VectorIndex(cfg.vector_index_dir, dim=cfg.embedding.dimension)
            emb = _embed(cfg, query)
            vec_hits = vec.search(emb, limit=limit)
        except Exception as e:  # noqa: BLE001
            vec_hits = []
            if mode == "vector":
                raise RuntimeError(f"vector search failed: {e}") from e

    # Reciprocal Rank Fusion
    rrf_k = 60
    fused: dict[tuple[str, str], SearchHit] = {}

    def _key(h: Any) -> tuple[str, str]:
        return (h.doc_id, h.chunk_id)

    for rank, h in enumerate(ft_hits, start=1):
        k = _key(h)
        hit = fused.setdefault(
            k,
            SearchHit(
                doc_id=h.doc_id, chunk_id=h.chunk_id, score=0.0,
                snippet=getattr(h, "snippet", "") or "",
                source_path=h.source_path, processed_path=h.processed_path,
                title=h.title,
            ),
        )
        hit.score += 1.0 / (rrf_k + rank)
        hit.via.append("fulltext")

    for rank, h in enumerate(vec_hits, start=1):
        k = _key(h)
        snippet = (getattr(h, "text", "") or "")[:280]
        hit = fused.setdefault(
            k,
            SearchHit(
                doc_id=h.doc_id, chunk_id=h.chunk_id, score=0.0,
                snippet=snippet,
                source_path=h.source_path, processed_path=h.processed_path,
                title=h.title,
            ),
        )
        hit.score += 1.0 / (rrf_k + rank)
        if not hit.snippet:
            hit.snippet = snippet
        hit.via.append("vector")

    ranked = sorted(fused.values(), key=lambda x: x.score, reverse=True)[:limit]
    return [h.to_dict() for h in ranked]
