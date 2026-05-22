"""Tantivy full-text index wrapper.

Schema:
  doc_id        STRING  stored, indexed (exact match for deletion)
  chunk_id      STRING  stored
  source_path   STRING  stored
  processed_path STRING stored
  title         TEXT    stored, indexed (jieba-tokenized at write/query time)
  title_raw     STRING  stored only — original title for display
  tags          STRING  stored, indexed (per-tag exact)
  confidentiality STRING stored, indexed
  type          STRING  stored, indexed
  text          TEXT    stored, indexed (jieba-tokenized at write/query time)
  text_raw      STRING  stored only — original chunk text for snippet display

Tokenization strategy:
  Tantivy's default tokenizer does not segment CJK text, so Chinese queries
  degrade to character-level or whole-string matches. We pre-tokenize `title`
  and `text` with jieba at index time and apply the same tokenizer to incoming
  queries before passing them to ``Index.parse_query``. The result is a space-
  separated stream of tokens that Tantivy's default tokenizer then indexes
  word-by-word — giving us proper CJK word boundaries while leaving ASCII
  tokens (English, numbers, identifiers, paths) untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    import tantivy  # type: ignore
except ImportError:  # pragma: no cover - allow import without dep at config time
    tantivy = None  # type: ignore

try:
    # jieba 0.42.1 imports the deprecated ``pkg_resources`` API in its
    # ``_compat`` module and triggers a UserWarning under Setuptools >= 81.
    # Upstream hasn't released a fix yet (see fxsjy/jieba issues); silence
    # only this specific warning around the import so it doesn't pollute
    # every ingest/search run.
    import warnings

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"pkg_resources is deprecated as an API.*",
            category=UserWarning,
        )
        import jieba  # type: ignore

    # Disable jieba's startup log spam; keep default dictionary.
    jieba.setLogLevel(60)  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    jieba = None  # type: ignore


# Bumped from fulltext-v1 when jieba pre-tokenization was introduced; any
# previously built index needs to be rebuilt to benefit from CJK segmentation.
SCHEMA_VERSION = "fulltext-v2"


_WS_RE = re.compile(r"\s+")


def _tokenize_for_index(text: str) -> str:
    """Segment text with jieba and rejoin with single spaces.

    jieba's ``cut`` keeps ASCII runs (English words, numbers, identifiers,
    file paths) as single tokens and splits CJK runs into words. Joining with
    spaces lets Tantivy's default tokenizer index each segment as one term.
    """
    if not text:
        return ""
    if jieba is None:
        return text
    tokens = [t for t in jieba.cut(text, cut_all=False) if t and not t.isspace()]
    return " ".join(tokens) if tokens else ""


def _tokenize_for_query(text: str) -> str:
    """Tokenize a query string with the same scheme used at index time."""
    return _WS_RE.sub(" ", _tokenize_for_index(text)).strip()


@dataclass
class FtHit:
    doc_id: str
    chunk_id: str
    score: float
    snippet: str
    source_path: str
    processed_path: str
    title: str = ""


def _require_tantivy() -> None:
    if tantivy is None:
        raise RuntimeError("tantivy is not installed. Run: pip install tantivy")


def _build_schema():  # type: ignore[no-untyped-def]
    _require_tantivy()
    builder = tantivy.SchemaBuilder()
    builder.add_text_field("doc_id", stored=True, tokenizer_name="raw")
    builder.add_text_field("chunk_id", stored=True, tokenizer_name="raw")
    builder.add_text_field("source_path", stored=True, tokenizer_name="raw")
    builder.add_text_field("processed_path", stored=True, tokenizer_name="raw")
    builder.add_text_field("title", stored=True)
    builder.add_text_field("title_raw", stored=True, tokenizer_name="raw")
    builder.add_text_field("tags", stored=True, tokenizer_name="raw")
    builder.add_text_field("confidentiality", stored=True, tokenizer_name="raw")
    builder.add_text_field("type", stored=True, tokenizer_name="raw")
    builder.add_text_field("text", stored=True)
    # Stored-only original text for snippet display (uses the "raw" tokenizer
    # so the whole chunk lands as a single non-searchable token; we never
    # query against this field).
    builder.add_text_field("text_raw", stored=True, tokenizer_name="raw")
    return builder.build()


class FullTextIndex:
    def __init__(self, path: Path):
        _require_tantivy()
        path.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.schema = _build_schema()
        self.index = tantivy.Index(self.schema, path=str(path))

    def delete_doc(self, doc_id: str) -> None:
        writer = self.index.writer(heap_size=50_000_000)
        writer.delete_documents("doc_id", doc_id)
        writer.commit()

    def add_chunks(self, doc_id: str, chunks: Iterable[dict[str, Any]]) -> None:
        writer = self.index.writer(heap_size=50_000_000)
        writer.delete_documents("doc_id", doc_id)
        for c in chunks:
            doc = tantivy.Document()
            doc.add_text("doc_id", doc_id)
            doc.add_text("chunk_id", c["chunk_id"])
            doc.add_text("source_path", c.get("source_path", ""))
            doc.add_text("processed_path", c.get("processed_path", ""))
            # `title` and `text` are tokenized with jieba so Tantivy's default
            # tokenizer indexes CJK on word boundaries. `source_path` etc. stay
            # raw because they use the "raw" tokenizer for exact match.
            doc.add_text("title", _tokenize_for_index(c.get("title", "")))
            doc.add_text("title_raw", c.get("title", ""))
            for t in c.get("tags", []) or []:
                doc.add_text("tags", t)
            doc.add_text("confidentiality", c.get("confidentiality", ""))
            doc.add_text("type", c.get("type", ""))
            doc.add_text("text", _tokenize_for_index(c.get("text", "")))
            doc.add_text("text_raw", c.get("text", ""))
            writer.add_document(doc)
        writer.commit()

    def search(self, query: str, limit: int = 10) -> list[FtHit]:
        self.index.reload()
        searcher = self.index.searcher()
        # Apply the same jieba tokenization to the query so that CJK queries
        # hit the segmented terms stored in the index.
        tokenized = _tokenize_for_query(query)
        if not tokenized:
            return []
        parsed = self.index.parse_query(tokenized, ["title", "text"])
        results = searcher.search(parsed, limit=limit).hits
        hits: list[FtHit] = []
        for score, doc_addr in results:
            doc = searcher.doc(doc_addr)
            # Prefer the original (un-tokenized) text for snippets so users see
            # readable Chinese rather than a space-separated token stream.
            text = doc.get_first("text_raw") or doc.get_first("text") or ""
            snippet = text[:280]
            hits.append(
                FtHit(
                    doc_id=doc.get_first("doc_id") or "",
                    chunk_id=doc.get_first("chunk_id") or "",
                    score=float(score),
                    snippet=snippet,
                    source_path=doc.get_first("source_path") or "",
                    processed_path=doc.get_first("processed_path") or "",
                    title=doc.get_first("title_raw") or doc.get_first("title") or "",
                )
            )
        return hits
