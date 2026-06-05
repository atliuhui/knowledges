"""Knowledges MCP Server entry point (FastMCP edition).

Exposes read-only tools by default. Maintenance tools (scan/convert/ingest/rebuild)
are gated behind `mcp.enable_maintenance_tools` in config.yaml.

Tool list:
  Read-only:
    kb_search
    kb_get_document
    kb_get_chunk
    kb_get_metadata
    kb_list_documents
    kb_list_tags
    kb_list_data_tables
    kb_read_data_table
    kb_query_data
    kb_suggest_metadata
    kb_preview_metadata_update
    kb_apply_metadata_update
    kb_bulk_preview_metadata_update
    kb_bulk_apply_metadata_update
    kb_warmup
    kb_warmup_status
  Maintenance (optional):
    kb_scan
    kb_convert
    kb_ingest
    kb_rebuild
    kb_run_pipeline
"""

from __future__ import annotations

import functools
import json
import logging
import subprocess
import sys
import threading
import time
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
from services.data_query import DataQueryError, read_table_rows, run_query
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

mcp = FastMCP("knowledges")

_WARMUP_LOCK = threading.Lock()
_WARMUP_THREAD: threading.Thread | None = None
_WARMUP_STATE: dict[str, Any] = {
    "status": "idle",
    "run_id": 0,
    "started_at_ms": None,
    "finished_at_ms": None,
    "updated_at_ms": None,
    "current_stage": None,
    "completed_stages": 0,
    "total_stages": 4,
    "progress_pct": 0.0,
    "ok": None,
    "total_ms": 0.0,
    "stages": [],
}


def _warmup_snapshot() -> dict[str, Any]:
    with _WARMUP_LOCK:
        return {
            **_WARMUP_STATE,
            "stages": [dict(s) for s in _WARMUP_STATE.get("stages", [])],
        }


def _is_ollama_model_loaded(cfg: Config) -> bool:
    """Return True when the configured Ollama embedding model is currently loaded.

    If this check fails for any reason, return False so `kb_warmup` can safely
    trigger a fresh warmup run.
    """
    try:
        import ollama  # type: ignore
    except ImportError:
        return False

    try:
        client = ollama.Client(host=cfg.embedding.host) if cfg.embedding.host else ollama.Client()
        resp = client.ps()
        models = resp.get("models", [])
        target = str(cfg.embedding.model).strip().lower()
        for item in models:
            name = str(item.get("name", "")).strip().lower()
            if name == target:
                return True
        return False
    except Exception:  # noqa: BLE001
        return False


def _run_warmup_once(
    cfg: Config,
    on_stage_update=None,  # noqa: ANN001
) -> dict[str, Any]:
    stages: list[dict[str, Any]] = []

    def _stage(name: str, fn):  # noqa: ANN001
        if on_stage_update is not None:
            on_stage_update(name, "started", None)
        t0 = time.perf_counter()
        try:
            detail = fn() or {}
            stage_result = {
                "stage": name,
                "ok": True,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                **detail,
            }
            stages.append(stage_result)
            if on_stage_update is not None:
                on_stage_update(name, "finished", stage_result)
        except Exception as e:  # noqa: BLE001
            LOG.exception("warmup stage %s failed", name)
            stage_result = {
                "stage": name,
                "ok": False,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                "error": str(e),
                "type": type(e).__name__,
            }
            stages.append(stage_result)
            if on_stage_update is not None:
                on_stage_update(name, "finished", stage_result)

    def _warm_jieba():
        from services.fulltext_tantivy import _tokenize_for_query  # type: ignore
        # Force jieba's lazy dictionary load.
        _tokenize_for_query("棰勭儹 warmup")
        return {}

    def _warm_fulltext():
        from services.fulltext_tantivy import FullTextIndex
        ft = FullTextIndex(cfg.fulltext_index_dir)
        # Hit the searcher once so Tantivy mmaps and parses schema.
        try:
            ft.search("warmup", limit=1)
        except Exception:  # noqa: BLE001
            # Empty index is fine for warmup.
            pass
        return {}

    def _warm_vector():
        from services.vector_lancedb import VectorIndex
        VectorIndex(cfg.vector_index_dir, dim=cfg.embedding.dimension)
        return {"dim": cfg.embedding.dimension}

    def _warm_embedder():
        # Re-use the hybrid_search module cache so the very next kb_search
        # query reuses the same OllamaEmbedder instance.
        from services import hybrid_search as hs
        from services.embeddings import OllamaEmbedder

        embedder = getattr(hs, "_EMBEDDER", None)
        if embedder is None:
            embedder = OllamaEmbedder(cfg.embedding)
            hs._EMBEDDER = embedder  # type: ignore[attr-defined]
        vec = embedder.embed_query("warmup")
        return {
            "model": cfg.embedding.model,
            "vector_len": len(vec),
            "keep_alive": cfg.embedding.keep_alive,
        }

    _stage("jieba", _warm_jieba)
    _stage("fulltext", _warm_fulltext)
    _stage("vector", _warm_vector)
    _stage("embedder", _warm_embedder)

    ok = all(s["ok"] for s in stages)
    total_ms = round(sum(s["elapsed_ms"] for s in stages), 1)
    return {"ok": ok, "total_ms": total_ms, "stages": stages}


def _warmup_worker(run_id: int) -> None:
    global _WARMUP_THREAD

    started_at_ms = round(time.time() * 1000, 1)

    def _on_stage_update(stage_name: str, event: str, stage_result: dict[str, Any] | None) -> None:
        with _WARMUP_LOCK:
            if _WARMUP_STATE.get("run_id") != run_id:
                return
            now_ms = round(time.time() * 1000, 1)
            if event == "started":
                _WARMUP_STATE["current_stage"] = stage_name
                _WARMUP_STATE["updated_at_ms"] = now_ms
            elif event == "finished" and stage_result is not None:
                completed = int(_WARMUP_STATE.get("completed_stages", 0)) + 1
                total = max(1, int(_WARMUP_STATE.get("total_stages", 4)))
                _WARMUP_STATE["completed_stages"] = completed
                _WARMUP_STATE["progress_pct"] = round((completed / total) * 100, 1)
                _WARMUP_STATE["stages"] = [
                    *_WARMUP_STATE.get("stages", []),
                    stage_result,
                ]
                _WARMUP_STATE["updated_at_ms"] = now_ms

    try:
        result = _run_warmup_once(_cfg(), on_stage_update=_on_stage_update)
    except Exception as e:  # noqa: BLE001
        LOG.exception("warmup run %s crashed", run_id)
        result = {
            "ok": False,
            "total_ms": 0.0,
            "stages": [{
                "stage": "internal",
                "ok": False,
                "elapsed_ms": 0.0,
                "error": str(e),
                "type": type(e).__name__,
            }],
        }

    finished_at_ms = round(time.time() * 1000, 1)
    with _WARMUP_LOCK:
        _WARMUP_STATE.update({
            "status": "succeeded" if result["ok"] else "failed",
            "run_id": run_id,
            "started_at_ms": started_at_ms,
            "finished_at_ms": finished_at_ms,
            "updated_at_ms": finished_at_ms,
            "current_stage": None,
            "completed_stages": len(result["stages"]),
            "total_stages": len(result["stages"]) or 4,
            "progress_pct": 100.0,
            "ok": result["ok"],
            "total_ms": result["total_ms"],
            "stages": result["stages"],
        })
        _WARMUP_THREAD = None


# ---------- Read-only tools ----------

@mcp.tool(
    name="kb_search",
    description="Hybrid (full-text + vector) search over the knowledge base.",
)
@_safe
def kb_search(query: str, mode: str | None = None, limit: int = 10) -> dict[str, Any]:
    cfg = _cfg()
    mode = mode or cfg.mcp.default_search_mode
    hits = hybrid_search(cfg, query=query, mode=mode, limit=limit)
    return {"query": query, "mode": mode, "hits": hits}


@mcp.tool(
    name="kb_get_document",
    description="Return a document's metadata plus its processed text.",
)
@_safe
def kb_get_document(id: str) -> dict[str, Any]:
    cfg = _cfg()
    rows = md.load_csv(cfg.docs_data)
    row = next((r for r in rows if r.id == id), None)
    if not row:
        raise ValueError(f"document not found: {id}")
    store = Database(cfg.db_data)
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
    name="kb_get_chunk",
    description="Return a specific chunk's text.",
)
@_safe
def kb_get_chunk(doc_id: str, chunk_id: str) -> dict[str, Any]:
    from services.chunking import chunk_text

    cfg = _cfg()
    store = Database(cfg.db_data)
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
    name="kb_get_metadata",
    description="Return CSV + runtime fields for a document (no processed text).",
)
@_safe
def kb_get_metadata(id: str) -> dict[str, Any]:
    cfg = _cfg()
    rows = md.load_csv(cfg.docs_data)
    row = next((r for r in rows if r.id == id), None)
    if not row:
        raise ValueError(f"document not found: {id}")
    store = Database(cfg.db_data)
    rec = store.get(id)
    return {
        "csv": {k: getattr(row, k) for k in md.FIELDS},
        "runtime": rec.__dict__ if rec else None,
    }


@mcp.tool(
    name="kb_list_documents",
    description="List/filter documents by status, tags, confidentiality, etc.",
)
@_safe
def kb_list_documents(
    filter: dict[str, Any] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    cfg = _cfg()
    rows = md.load_csv(cfg.docs_data)
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
    name="kb_list_tags",
    description="List all tags with usage counts.",
)
@_safe
def kb_list_tags() -> dict[str, Any]:
    cfg = _cfg()
    rows = md.load_csv(cfg.docs_data)
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
    name="kb_list_data_tables",
    description=(
        "List Parquet-backed data tables in the knowledge base. Optionally "
        "filter by doc_id. Each entry includes table_name, source document, "
        "parquet path (relative to kb_root), columns and row_count."
    ),
)
@_safe
def kb_list_data_tables(doc_id: str | None = None) -> dict[str, Any]:
    cfg = _cfg()
    store = Database(cfg.db_data)
    rows = store.list_data_tables(doc_id=doc_id)
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            cols = json.loads(r.get("columns_json") or "[]")
        except Exception:  # noqa: BLE001
            cols = []
        out.append({
            "table_name": r.get("table_name"),
            "doc_id": r.get("doc_id"),
            "source_path": r.get("source_path"),
            "sheet": r.get("sheet"),
            "parquet_path": r.get("parquet_path"),
            "columns": cols,
            "row_count": r.get("row_count"),
            "pipeline_version": r.get("pipeline_version"),
            "created_at": r.get("created_at"),
        })
    return {"data_tables": out, "count": len(out)}


@mcp.tool(
    name="kb_read_data_table",
    description=(
        "Read a page of rows from a registered data table by table_name. "
        "Returns column names and row dicts. Default limit=50, max 1000."
    ),
)
@_safe
def kb_read_data_table(
    table_name: str,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    cfg = _cfg()
    if limit <= 0 or limit > 1000:
        raise ValueError("limit must be in 1..1000")
    if offset < 0:
        raise ValueError("offset must be >= 0")
    store = Database(cfg.db_data)
    row = store.get_data_table(table_name)
    if not row:
        raise ValueError(f"data table not found: {table_name}")
    return read_table_rows(
        cfg.knowledge_base_root, row, limit=limit, offset=offset,
    )


@mcp.tool(
    name="kb_query_data",
    description=(
        "Run a read-only SELECT/WITH SQL query over the registered Parquet "
        "data tables (each registered row exposed as a DuckDB view named by "
        "table_name). Only one statement is permitted; mutation/extension "
        "keywords are rejected. A LIMIT cap is applied when the user query "
        "does not contain LIMIT."
    ),
)
@_safe
def kb_query_data(sql: str, limit: int = 1000) -> dict[str, Any]:
    cfg = _cfg()
    if limit <= 0 or limit > 10000:
        raise ValueError("limit must be in 1..10000")
    store = Database(cfg.db_data)
    rows = store.list_data_tables()
    try:
        return run_query(cfg.knowledge_base_root, rows, sql, hard_cap=limit)
    except DataQueryError as e:
        return {"error": str(e), "type": "DataQueryError"}


@mcp.tool(
    name="kb_suggest_metadata",
    description="Suggest document fields (heuristic).",
)
@_safe
def kb_suggest_metadata(id: str) -> dict[str, Any]:
    cfg = _cfg()
    rows = md.load_csv(cfg.docs_data)
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
        if segment.endswith((
            ".docx", ".pdf", ".pptx", ".md", ".xlsx", ".txt", ".csv", ".drawio", ".xmind"
        )):
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
    name="kb_preview_metadata_update",
    description="Preview a single-document field patch. Does not write.",
)
@_safe
def kb_preview_metadata_update(id: str, patch: dict[str, Any]) -> dict[str, Any]:
    return preview_update(_cfg(), id, patch or {})


@mcp.tool(
    name="kb_apply_metadata_update",
    description="Apply a single-document field patch after user confirmation.",
)
@_safe
def kb_apply_metadata_update(id: str, patch: dict[str, Any]) -> dict[str, Any]:
    return apply_update(_cfg(), id, patch or {})


@mcp.tool(
    name="kb_bulk_preview_metadata_update",
    description="Preview a bulk document update operation.",
)
@_safe
def kb_bulk_preview_metadata_update(
    ids: list[str],
    operation: dict[str, Any],
) -> dict[str, Any]:
    return bulk_preview(_cfg(), ids or [], BulkOperation(**(operation or {})))


@mcp.tool(
    name="kb_bulk_apply_metadata_update",
    description="Apply a bulk document update operation after user confirmation.",
)
@_safe
def kb_bulk_apply_metadata_update(
    ids: list[str],
    operation: dict[str, Any],
) -> dict[str, Any]:
    return bulk_apply(_cfg(), ids or [], BulkOperation(**(operation or {})))


# ---------- H5 offline apps lane ----------

from services import apps as apps_svc  # noqa: E402  (kept near related tools)


@mcp.tool(
    name="kb_create_app",
    description=(
        "Create or overwrite an HTML5 offline app under <kb_root>/apps/<slug>/. "
        "`files` maps relative POSIX paths (e.g. 'index.html', 'js/app.js') to "
        "text content. Returns the local URL served by miniserve so the agent "
        "can advertise a clickable link in chat. Slug must match [a-z0-9][a-z0-9-]*."
    ),
)
@_safe
def kb_create_app(
    slug: str,
    files: dict[str, str],
    overwrite: bool = False,
) -> dict[str, Any]:
    result = apps_svc.create_app(_cfg(), slug, files, overwrite=overwrite)
    return result.to_dict()


@mcp.tool(
    name="kb_list_apps",
    description=(
        "List H5 offline apps stored under <kb_root>/apps/. Each entry includes "
        "the slug, local URL, on-disk path, file count and total size."
    ),
)
@_safe
def kb_list_apps() -> dict[str, Any]:
    return {"apps": apps_svc.list_apps(_cfg())}


@mcp.tool(
    name="kb_warmup",
    description=(
        "Start a background warmup job that pre-loads expensive components "
        "(Tantivy, LanceDB, jieba dictionary, and Ollama embeddings). Returns "
        "immediately to avoid client timeout. Use kb_warmup_status to inspect "
        "progress and stage results."
    ),
)
@_safe
def kb_warmup(refresh: bool = False) -> dict[str, Any]:
    global _WARMUP_THREAD

    cfg = _cfg()

    with _WARMUP_LOCK:
        status = _WARMUP_STATE["status"]

        if status == "running":
            return {
                "ok": True,
                "status": "running",
                "run_id": _WARMUP_STATE["run_id"],
                "started_at_ms": _WARMUP_STATE["started_at_ms"],
                "message": "Warmup is already running. Use kb_warmup_status.",
            }

        if status == "succeeded" and not refresh:
            if _is_ollama_model_loaded(cfg):
                return {
                    "ok": True,
                    "status": "succeeded",
                    "cached": True,
                    "run_id": _WARMUP_STATE["run_id"],
                    "total_ms": _WARMUP_STATE["total_ms"],
                    "stages": [dict(s) for s in _WARMUP_STATE.get("stages", [])],
                    "message": "Warmup already completed. Pass refresh=true to run again.",
                }
            LOG.info("Warmup cache invalidated: Ollama model appears unloaded; restarting warmup.")

        run_id = int(_WARMUP_STATE.get("run_id", 0)) + 1
        _WARMUP_STATE.update({
            "status": "running",
            "run_id": run_id,
            "started_at_ms": round(time.time() * 1000, 1),
            "finished_at_ms": None,
            "updated_at_ms": round(time.time() * 1000, 1),
            "current_stage": None,
            "completed_stages": 0,
            "total_stages": 4,
            "progress_pct": 0.0,
            "ok": None,
            "total_ms": 0.0,
            "stages": [],
        })

        _WARMUP_THREAD = threading.Thread(
            target=_warmup_worker,
            args=(run_id,),
            name=f"kb-warmup-{run_id}",
            daemon=True,
        )
        _WARMUP_THREAD.start()

    return {
        "ok": True,
        "status": "started",
        "run_id": run_id,
        "message": "Warmup started in background. Use kb_warmup_status for results.",
    }


@mcp.tool(
    name="kb_warmup_status",
    description="Get status/result for the latest background warmup run.",
)
@_safe
def kb_warmup_status() -> dict[str, Any]:
    return _warmup_snapshot()


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
        name="kb_scan",
        description="Run tools.scan to refresh store/docs.csv.",
    )
    mcp.add_tool(
        kb_convert,
        name="kb_convert",
        description="Run tools.convert to refresh text/ and runtime metadata.",
    )
    mcp.add_tool(
        kb_ingest,
        name="kb_ingest",
        description="Run tools.ingest to refresh full-text + vector indexes.",
    )
    mcp.add_tool(
        kb_rebuild,
        name="kb_rebuild",
        description="Run scan + convert + ingest sequentially.",
    )
    mcp.add_tool(
        kb_run_pipeline,
        name="kb_run_pipeline",
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
    LOG.info("Knowledges MCP server starting (kb_root=%s)", _CFG.knowledge_base_root)

    if _CFG.mcp.enable_maintenance_tools:
        _register_maintenance_tools()
        LOG.info("Maintenance tools enabled.")

    # FastMCP defaults to stdio transport when run via mcp.run().
    mcp.run()


if __name__ == "__main__":
    main()
