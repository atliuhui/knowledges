"""KnowledgeBase MCP Server entry point (FastMCP edition).

Exposes read-only tools by default. Maintenance tools (scan/convert/ingest/rebuild)
are gated behind `mcp.enable_maintenance_tools` in config.yaml.

Tool list:
  Read-only:
    kb.search
    kb.get_document
    kb.get_chunk
    kb.get_metadata
    kb.list_documents
    kb.list_tags
    kb.suggest_metadata
    kb.preview_metadata_update
    kb.apply_metadata_update
    kb.bulk_preview_metadata_update
    kb.bulk_apply_metadata_update
  Maintenance (optional):
    kb.scan
    kb.convert
    kb.ingest
    kb.rebuild
    kb.run_pipeline
"""

from __future__ import annotations

import functools
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP  # type: ignore
except ImportError as e:  # pragma: no cover
    raise RuntimeError(
        "The `mcp` package is required to run server.py. Install with: pip install mcp"
    ) from e

from services import metadata as md
from services.config import Config, load_config
from services.hybrid_search import search as hybrid_search
from services.metadata_editing import (
    BulkOperation,
    apply_update,
    bulk_apply,
    bulk_preview,
    preview_update,
)
from services.database import Database

LOG = logging.getLogger("kb.server")

# Module-level config singleton. Populated by main() before mcp.run().
_CFG: Config | None = None


def _cfg() -> Config:
    if _CFG is None:
        raise RuntimeError("Config not initialized; server.main() must run first.")
    return _CFG


def _safe(fn):
    """Wrap a tool function so exceptions are returned as JSON instead of MCP errors.

    Preserves the legacy contract where tool failures appear as
    ``{"error": ..., "type": ...}`` in the tool result.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            LOG.exception("tool %s failed", fn.__name__)
            return {"error": str(e), "type": type(e).__name__}

    return wrapper


# ---------- FastMCP instance ----------

mcp = FastMCP("knowledgebase")


# ---------- Read-only tools ----------

@mcp.tool(
    name="kb.search",
    description="Hybrid (full-text + vector) search over the knowledge base.",
)
@_safe
def kb_search(query: str, mode: str | None = None, limit: int = 10) -> dict[str, Any]:
    cfg = _cfg()
    mode = mode or cfg.mcp.default_search_mode
    hits = hybrid_search(cfg, query=query, mode=mode, limit=limit)
    return {"query": query, "mode": mode, "hits": hits}


@mcp.tool(
    name="kb.get_document",
    description="Return a document's metadata plus its processed text.",
)
@_safe
def kb_get_document(id: str) -> dict[str, Any]:
    cfg = _cfg()
    rows = md.load_csv(cfg.documents_data)
    row = next((r for r in rows if r.id == id), None)
    if not row:
        raise ValueError(f"document not found: {id}")
    store = Database(cfg.database_data)
    rec = store.get(id)
    text = ""
    if rec and rec.processed_path:
        p = cfg.knowledge_base_root / rec.processed_path
        if p.exists():
            text = p.read_text(encoding="utf-8", errors="replace")
    return {
        "id": row.id,
        "title": row.title,
        "source_path": row.source_path,
        "processed_path": rec.processed_path if rec else None,
        "type": row.type,
        "tags": row.tag_list(),
        "confidentiality": row.confidentiality,
        "status": row.status,
        "notes": row.notes,
        "processed_text": text,
    }


@mcp.tool(
    name="kb.get_chunk",
    description="Return a specific chunk's text.",
)
@_safe
def kb_get_chunk(doc_id: str, chunk_id: str) -> dict[str, Any]:
    from services.chunking import chunk_text

    cfg = _cfg()
    store = Database(cfg.database_data)
    rec = store.get(doc_id)
    if not rec or not rec.processed_path:
        raise ValueError(f"document not converted: {doc_id}")
    p = cfg.knowledge_base_root / rec.processed_path
    if not p.exists():
        raise ValueError(f"processed file missing: {p}")
    text = p.read_text(encoding="utf-8", errors="replace")
    chunks = chunk_text(text, cfg.chunking.max_chars, cfg.chunking.overlap_chars)
    for c in chunks:
        if c.chunk_id == chunk_id:
            return {"doc_id": doc_id, "chunk_id": chunk_id, "text": c.text}
    raise ValueError(f"chunk not found: {chunk_id}")


@mcp.tool(
    name="kb.get_metadata",
    description="Return CSV + runtime fields for a document (no processed text).",
)
@_safe
def kb_get_metadata(id: str) -> dict[str, Any]:
    cfg = _cfg()
    rows = md.load_csv(cfg.documents_data)
    row = next((r for r in rows if r.id == id), None)
    if not row:
        raise ValueError(f"document not found: {id}")
    store = Database(cfg.database_data)
    rec = store.get(id)
    return {
        "csv": {k: getattr(row, k) for k in md.FIELDS},
        "runtime": rec.__dict__ if rec else None,
    }


@mcp.tool(
    name="kb.list_documents",
    description="List/filter documents by status, tags, confidentiality, etc.",
)
@_safe
def kb_list_documents(
    filter: dict[str, Any] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    cfg = _cfg()
    rows = md.load_csv(cfg.documents_data)
    flt = filter or {}
    if "status" in flt:
        rows = [r for r in rows if r.status in set(flt["status"])]
    if flt.get("missing_tags"):
        rows = [r for r in rows if not r.tag_list()]
    if flt.get("missing_title"):
        rows = [r for r in rows if not r.title]
    if "tags_include" in flt:
        need = set(flt["tags_include"])
        rows = [r for r in rows if need.issubset(set(r.tag_list()))]
    if "tags_exclude" in flt:
        bad = set(flt["tags_exclude"])
        rows = [r for r in rows if not (bad & set(r.tag_list()))]
    if "confidentiality" in flt:
        rows = [r for r in rows if r.confidentiality in set(flt["confidentiality"])]
    rows = rows[offset : offset + limit]
    return {
        "documents": [
            {
                "id": r.id,
                "title": r.title,
                "source_path": r.source_path,
                "type": r.type,
                "tags": r.tag_list(),
                "confidentiality": r.confidentiality,
                "status": r.status,
                "notes": r.notes,
            }
            for r in rows
        ]
    }


@mcp.tool(
    name="kb.list_tags",
    description="List all tags with usage counts.",
)
@_safe
def kb_list_tags() -> dict[str, Any]:
    cfg = _cfg()
    rows = md.load_csv(cfg.documents_data)
    counts: dict[str, int] = {}
    for r in rows:
        for t in r.tag_list():
            counts[t] = counts.get(t, 0) + 1
    return {
        "tags": [
            {"tag": t, "count": c}
            for t, c in sorted(counts.items(), key=lambda x: -x[1])
        ]
    }


@mcp.tool(
    name="kb.suggest_metadata",
    description="Suggest document fields (heuristic).",
)
@_safe
def kb_suggest_metadata(id: str) -> dict[str, Any]:
    cfg = _cfg()
    rows = md.load_csv(cfg.documents_data)
    row = next((r for r in rows if r.id == id), None)
    if not row:
        raise ValueError(f"document not found: {id}")
    path = row.source_path.lower().replace("\\", "/")
    suggested_type = ""
    if "proposal" in path:
        suggested_type = "proposal"
    elif "meeting" in path or "review" in path:
        suggested_type = "meeting"
    elif "policy" in path:
        suggested_type = "policy"
    elif path.endswith((".md", ".txt")):
        suggested_type = "note"

    suggested_tags: list[str] = []
    for segment in path.split("/"):
        if "/" in segment:
            continue
        if segment.endswith((".docx", ".pdf", ".pptx", ".md", ".xlsx", ".txt", ".csv")):
            continue
        if segment and segment not in {"documents"}:
            suggested_tags.append(segment)

    return {
        "doc_id": id,
        "suggested": {
            "type": suggested_type,
            "tags": suggested_tags,
            "confidentiality": row.confidentiality or "internal",
        },
        "confidence": 0.5,
        "reason": "Heuristic suggestion derived from file path segments.",
    }


@mcp.tool(
    name="kb.preview_metadata_update",
    description="Preview a single-document field patch. Does not write.",
)
@_safe
def kb_preview_metadata_update(id: str, patch: dict[str, Any]) -> dict[str, Any]:
    return preview_update(_cfg(), id, patch or {})


@mcp.tool(
    name="kb.apply_metadata_update",
    description="Apply a single-document field patch after user confirmation.",
)
@_safe
def kb_apply_metadata_update(id: str, patch: dict[str, Any]) -> dict[str, Any]:
    return apply_update(_cfg(), id, patch or {})


@mcp.tool(
    name="kb.bulk_preview_metadata_update",
    description="Preview a bulk document update operation.",
)
@_safe
def kb_bulk_preview_metadata_update(
    ids: list[str],
    operation: dict[str, Any],
) -> dict[str, Any]:
    return bulk_preview(_cfg(), ids or [], BulkOperation(**(operation or {})))


@mcp.tool(
    name="kb.bulk_apply_metadata_update",
    description="Apply a bulk document update operation after user confirmation.",
)
@_safe
def kb_bulk_apply_metadata_update(
    ids: list[str],
    operation: dict[str, Any],
) -> dict[str, Any]:
    return bulk_apply(_cfg(), ids or [], BulkOperation(**(operation or {})))


# ---------- Maintenance tools (registered conditionally in main) ----------

def _run_tool_script(script: str, extra_args: list[str] | None = None) -> dict[str, Any]:
    cmd = [sys.executable, "-m", f"tools.{script}"]
    if extra_args:
        cmd.extend(extra_args)
    proc = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(Path(__file__).parent)
    )
    return {
        "tool": script,
        "exit_code": proc.returncode,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-2000:],
    }


@_safe
def kb_scan() -> dict[str, Any]:
    return _run_tool_script("scan")


@_safe
def kb_convert() -> dict[str, Any]:
    return _run_tool_script("convert")


@_safe
def kb_ingest() -> dict[str, Any]:
    return _run_tool_script("ingest")


@_safe
def kb_rebuild() -> dict[str, Any]:
    return {"steps": [_run_tool_script(s) for s in ("scan", "convert", "ingest")]}


@_safe
def kb_run_pipeline(
    force: bool = False,
    only: list[str] | None = None,
    no_vector: bool = False,
) -> dict[str, Any]:
    only = [x for x in (only or []) if x]
    common_only_args: list[str] = []
    for doc_id in only:
        common_only_args.extend(["--only", doc_id])

    scan_args: list[str] = []
    convert_args = (["--force"] if force else []) + common_only_args
    ingest_args = (["--force"] if force else []) + common_only_args + (["--no-vector"] if no_vector else [])

    steps = [
        _run_tool_script("scan", scan_args),
        _run_tool_script("convert", convert_args),
        _run_tool_script("ingest", ingest_args),
    ]
    ok = all(s.get("exit_code", 1) == 0 for s in steps)
    return {
        "ok": ok,
        "requested": {
            "force": force,
            "only": only,
            "no_vector": no_vector,
        },
        "steps": steps,
    }


def _register_maintenance_tools() -> None:
    mcp.add_tool(
        kb_scan,
        name="kb.scan",
        description="Run tools.scan to refresh index/documents.csv.",
    )
    mcp.add_tool(
        kb_convert,
        name="kb.convert",
        description="Run tools.convert to refresh processed/ and runtime metadata.",
    )
    mcp.add_tool(
        kb_ingest,
        name="kb.ingest",
        description="Run tools.ingest to refresh full-text + vector indexes.",
    )
    mcp.add_tool(
        kb_rebuild,
        name="kb.rebuild",
        description="Run scan + convert + ingest sequentially.",
    )
    mcp.add_tool(
        kb_run_pipeline,
        name="kb.run_pipeline",
        description=(
            "Run scan -> convert -> ingest with optional force/only/no_vector options."
        ),
    )


# ---------- Entry point ----------

def main() -> None:
    global _CFG
    _CFG = load_config()
    _CFG.ensure_dirs()
    logging.basicConfig(
        filename=str(_CFG.logs_dir / "server.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    LOG.info("KnowledgeBase MCP server starting (kb_root=%s)", _CFG.knowledge_base_root)

    if _CFG.mcp.enable_maintenance_tools:
        _register_maintenance_tools()
        LOG.info("Maintenance tools enabled.")

    # FastMCP defaults to stdio transport when run via mcp.run().
    mcp.run()


if __name__ == "__main__":
    main()
