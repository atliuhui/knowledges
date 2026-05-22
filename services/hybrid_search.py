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
    """Embed a query using the configured local model. Lazy import.

    For Qwen3-Embedding (and other instruction-tuned models), queries are
    encoded with a `query` prompt so that they align with passage embeddings.
    """
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "sentence-transformers not installed; cannot run vector search."
        ) from e
    # Cache model on the module for repeated queries.
    global _MODEL  # noqa: PLW0603
    try:
        model = _MODEL  # type: ignore[name-defined]
    except NameError:
        model = SentenceTransformer(cfg.embedding.model)
        _MODEL = model  # type: ignore[name-defined]
    raw = cfg.embedding
    encode_kwargs: dict[str, object] = {"normalize_embeddings": raw.normalize}
    if raw.query_prompt_name:
        try:
            vec = model.encode([text], prompt_name=raw.query_prompt_name, **encode_kwargs)[0]
        except (TypeError, ValueError):
            # Model does not define this prompt; fall back to plain encoding.
            vec = model.encode([text], **encode_kwargs)[0]
    else:
        vec = model.encode([text], **encode_kwargs)[0]
    return [float(x) for x in vec]


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
