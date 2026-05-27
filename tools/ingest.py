"""Build / update Tantivy full-text index and LanceDB vector index from processed/."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import click

from services import metadata as md
from services.chunking import chunk_text
from services.config import load_config
from services.database import Database

from tools._common import acquire_run_lock_or_exit, setup_logger


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _load_embedder(cfg):  # type: ignore[no-untyped-def]
    from services.embeddings import OllamaEmbedder
    return OllamaEmbedder(cfg.embedding)


@click.command()
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
@click.option("--kb-root", "kb_root", type=str, default=None)
@click.option("--force", is_flag=True, help="Re-index all converted docs.")
@click.option("--only", multiple=True, help="Limit to specific doc ids.")
@click.option("--no-vector", is_flag=True, help="Skip vector index (useful if no GPU/model).")
@click.option(
    "--concurrency",
    type=int,
    default=None,
    help="Number of parallel embedding requests per document. Defaults to config.ingest.concurrency.",
)
def main(config_path: Path | None, kb_root: str | None, force: bool,
         only: tuple[str, ...], no_vector: bool, concurrency: int | None) -> None:
    cfg = load_config(config_path=config_path, knowledge_base_root_override=kb_root)
    cfg.ensure_dirs()
    log = setup_logger("ingest", cfg.logs_dir / "ingest.log")
    log.info("ingest start: fulltext=%s vector=%s", cfg.fulltext_index_dir, cfg.vector_index_dir)

    with acquire_run_lock_or_exit(cfg, step="ingest", logger=log):
        _run_ingest(
            cfg, log,
            force=force, only=only, no_vector=no_vector,
            concurrency=concurrency,
        )


def _run_ingest(cfg, log, *, force: bool, only: tuple[str, ...], no_vector: bool,
                concurrency: int | None) -> None:
    rows = md.load_csv(cfg.docs_data)
    csv_by_id = {r.id: r for r in rows}
    store = Database(cfg.db_data)
    records = store.all()
    only_set = set(only) if only else None

    # Per-doc pending markers are written by tools.convert: a successful
    # (re)conversion sets fulltext_index_status / vector_index_status = "pending",
    # while a cached/no-op convert leaves the previous "ok" untouched. We pre-scan
    # those markers so that, when nothing changed, ingest exits before paying the
    # cost of opening Tantivy/LanceDB and loading the embedding model.
    def _doc_needs_work(rec) -> bool:  # noqa: ANN001
        if rec.convert_status != "ok" or not rec.processed_path:
            return False
        row = csv_by_id.get(rec.id)
        if row is None or row.status in {"ignored", "archived", "missing"}:
            return False
        if only_set is not None and rec.id not in only_set:
            return False
        if rec.fulltext_index_status != "ok":
            return True
        if not no_vector and rec.vector_index_status != "ok":
            return True
        return False

    pending_count = sum(1 for r in records if _doc_needs_work(r))
    if pending_count == 0 and not force:
        log.info("ingest skipped: no pending docs (convert marked nothing dirty)")
        return
    log.info("ingest pending=%d force=%s no_vector=%s", pending_count, force, no_vector)

    # Lazy-import index engines.
    from services.fulltext_tantivy import FullTextIndex, SCHEMA_VERSION as FT_VERSION
    ft = FullTextIndex(cfg.fulltext_index_dir)

    vec = None
    embedder = None
    if not no_vector:
        try:
            from services.vector_lancedb import VectorIndex, SCHEMA_VERSION as VEC_VERSION  # noqa: F401
            vec = VectorIndex(cfg.vector_index_dir, dim=cfg.embedding.dimension)
            embedder = _load_embedder(cfg)
        except Exception as e:  # noqa: BLE001
            log.warning("vector backend unavailable, continuing fulltext only: %s", e)
            vec = None
            embedder = None

    n_ok = n_skipped = n_failed = 0

    # Pre-filter records into a worklist so the thread pool knows the total.
    work: list = []
    for rec in records:
        if only_set is not None and rec.id not in only_set:
            continue
        if rec.convert_status != "ok" or not rec.processed_path:
            continue
        row = csv_by_id.get(rec.id)
        if row is None or row.status in {"ignored", "archived", "missing"}:
            continue
        needs_ft = force or rec.fulltext_index_status != "ok"
        needs_vec = (vec is not None) and (force or rec.vector_index_status != "ok")
        if not needs_ft and not needs_vec:
            n_skipped += 1
            continue
        work.append((rec, row, needs_ft, needs_vec))

    workers = concurrency if concurrency is not None else cfg.ingest.concurrency
    workers = max(1, int(workers))
    log.info("ingest embed_workers=%d items=%d", workers, len(work))

    # Aggregated phase timings (seconds) across all docs.
    phase_totals = {"read": 0.0, "chunk": 0.0, "embed": 0.0, "ft_write": 0.0, "vec_write": 0.0, "store": 0.0}

    # Shared thread pool used to fan out embedding requests across chunks of a
    # single document. Documents themselves are processed sequentially, so the
    # index/store backends (Tantivy, LanceDB, SQLite) never see concurrent
    # writers and need no extra locking.
    embed_pool: ThreadPoolExecutor | None = None
    if embedder is not None and workers > 1:
        embed_pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="embed")

    def _embed_chunks_parallel(texts: list[str]) -> list[list[float]]:
        """Embed a list of chunk texts, sharding across the embed pool.

        Order of returned vectors matches `texts`. With workers==1 (or no
        pool), falls back to a single batched call.
        """
        if not texts or embedder is None:
            return []
        if embed_pool is None or workers <= 1 or len(texts) <= 1:
            return embedder.embed_documents(texts)

        # Split into `workers` roughly-equal shards, preserving order.
        n = len(texts)
        k = min(workers, n)
        shard_size = (n + k - 1) // k
        shards: list[tuple[int, list[str]]] = []
        for i in range(0, n, shard_size):
            shards.append((i, texts[i:i + shard_size]))

        results: list[list[float]] = [None] * n  # type: ignore[list-item]
        futures = {
            embed_pool.submit(embedder.embed_documents, shard): start
            for start, shard in shards
        }
        for fut in futures:
            start = futures[fut]
            vecs = fut.result()
            for j, v in enumerate(vecs):
                results[start + j] = v
        return results

    def _process_one(rec, row, needs_ft, needs_vec):  # noqa: ANN001
        timings = {"read": 0.0, "chunk": 0.0, "embed": 0.0, "ft_write": 0.0, "vec_write": 0.0, "store": 0.0, "text_chars": 0}
        t_start = time.perf_counter()

        processed_abs = cfg.knowledge_base_root / rec.processed_path
        if not processed_abs.exists():
            log.error("FAIL processed missing: %s", rec.processed_path)
            store.update(
                rec.id, rec.source_path,
                fulltext_index_status="failed",
                fulltext_index_error="processed file missing",
                fulltext_indexed_at=_now(),
            )
            return rec.id, "failed", 0, timings, time.perf_counter() - t_start

        # --- read + chunk ---
        t0 = time.perf_counter()
        text = processed_abs.read_text(encoding="utf-8", errors="replace")
        t1 = time.perf_counter()
        chunks = chunk_text(
            text,
            max_chars=cfg.chunking.max_chars,
            overlap_chars=cfg.chunking.overlap_chars,
        )
        t2 = time.perf_counter()
        timings["read"] = t1 - t0
        timings["chunk"] = t2 - t1
        timings["text_chars"] = len(text)

        tags = row.tag_list()
        common_meta = {
            "title": row.title or row.source_path,
            "tags": tags,
            "type": row.type,
            "confidentiality": row.confidentiality,
            "source_path": row.source_path,
            "processed_path": rec.processed_path,
        }
        ft_payload = [
            {**common_meta, "chunk_id": c.chunk_id, "text": c.text}
            for c in chunks
        ]

        # --- embed (chunks parallelized across embed_pool) ---
        embeddings: list[list[float]] | None = None
        if needs_vec and vec is not None and embedder is not None:
            try:
                t3 = time.perf_counter()
                embeddings = _embed_chunks_parallel([c.text for c in chunks])
                timings["embed"] = time.perf_counter() - t3
            except Exception as e:  # noqa: BLE001
                log.error("EMBED FAIL %s: %s", rec.id, e)
                store.update(
                    rec.id, rec.source_path,
                    vector_index_status="failed",
                    vector_index_error=str(e),
                    vector_indexed_at=_now(),
                )
                return rec.id, "failed", len(chunks), timings, time.perf_counter() - t_start

        # --- fulltext write ---
        if needs_ft:
            try:
                t4 = time.perf_counter()
                ft.add_chunks(rec.id, ft_payload)
                t5 = time.perf_counter()
                store.update(
                    rec.id, rec.source_path,
                    chunking_version=cfg.chunking.version,
                    fulltext_engine=cfg.fulltext_engine,
                    fulltext_index_version=FT_VERSION,
                    fulltext_index_status="ok",
                    fulltext_index_error=None,
                    fulltext_indexed_at=_now(),
                )
                timings["ft_write"] = t5 - t4
                timings["store"] += time.perf_counter() - t5
            except Exception as e:  # noqa: BLE001
                log.error("FT  FAIL %s: %s", rec.id, e)
                store.update(
                    rec.id, rec.source_path,
                    fulltext_index_status="failed",
                    fulltext_index_error=str(e),
                    fulltext_indexed_at=_now(),
                )
                return rec.id, "failed", len(chunks), timings, time.perf_counter() - t_start

        # --- vector write ---
        if needs_vec and vec is not None and embeddings is not None:
            try:
                vec_payload = []
                for c, emb in zip(chunks, embeddings):
                    vec_payload.append({
                        "doc_id": rec.id,
                        "chunk_id": c.chunk_id,
                        "text": c.text,
                        "title": common_meta["title"],
                        "tags": tags,
                        "type": row.type,
                        "confidentiality": row.confidentiality,
                        "source_path": row.source_path,
                        "processed_path": rec.processed_path,
                        "processed_hash": rec.processed_hash or "",
                        "vector": [float(x) for x in emb],
                    })
                t6 = time.perf_counter()
                vec.add_chunks(rec.id, vec_payload)
                t7 = time.perf_counter()
                store.update(
                    rec.id, rec.source_path,
                    vector_engine=cfg.vector_engine,
                    embedding_model=cfg.embedding.model,
                    vector_index_version="vector-v1",
                    vector_index_status="ok",
                    vector_index_error=None,
                    vector_indexed_at=_now(),
                )
                timings["vec_write"] = t7 - t6
                timings["store"] += time.perf_counter() - t7
            except Exception as e:  # noqa: BLE001
                log.error("VEC FAIL %s: %s", rec.id, e)
                store.update(
                    rec.id, rec.source_path,
                    vector_index_status="failed",
                    vector_index_error=str(e),
                    vector_indexed_at=_now(),
                )
                return rec.id, "failed", len(chunks), timings, time.perf_counter() - t_start

        total = time.perf_counter() - t_start
        for k in phase_totals:
            phase_totals[k] += timings.get(k, 0.0)
        return rec.id, "ok", len(chunks), timings, total

    wall_start = time.perf_counter()
    try:
        for rec, row, needs_ft, needs_vec in work:
            doc_id, status, n_chunks, timings, total = _process_one(rec, row, needs_ft, needs_vec)
            if status == "ok":
                n_ok += 1
                chars = int(timings.get("text_chars", 0) or 0)
                embed_s = float(timings.get("embed", 0.0) or 0.0)
                chunks_per_s = (n_chunks / embed_s) if embed_s > 0.0 else 0.0
                kchars_per_s = ((chars / 1000.0) / embed_s) if embed_s > 0.0 and chars > 0 else 0.0
                ms_per_chunk = ((embed_s * 1000.0) / n_chunks) if n_chunks > 0 and embed_s > 0.0 else 0.0
                s_per_kchar = (embed_s / (chars / 1000.0)) if chars > 0 and embed_s > 0.0 else 0.0
                log.info(
                    "OK   id=%s chunks=%d chars=%d total=%.2fs | read=%.2f chunk=%.2f embed=%.2f ft=%.2f vec=%.2f store=%.2f | embed_throughput chunks/s=%.2f kchars/s=%.2f ms/chunk=%.1f s/kchar=%.2f",
                    doc_id, n_chunks, chars, total,
                    timings.get("read", 0.0), timings.get("chunk", 0.0), timings.get("embed", 0.0),
                    timings.get("ft_write", 0.0), timings.get("vec_write", 0.0), timings.get("store", 0.0),
                    chunks_per_s, kchars_per_s, ms_per_chunk, s_per_kchar,
                )
            else:
                n_failed += 1
    finally:
        if embed_pool is not None:
            embed_pool.shutdown(wait=True)
    wall = time.perf_counter() - wall_start

    log.info("ingest done: ok=%d skipped=%d failed=%d wall=%.2fs", n_ok, n_skipped, n_failed, wall)
    log.info(
        "ingest phase totals: read=%.2fs chunk=%.2fs embed=%.2fs ft_write=%.2fs vec_write=%.2fs store=%.2fs",
        phase_totals["read"], phase_totals["chunk"], phase_totals["embed"],
        phase_totals["ft_write"], phase_totals["vec_write"], phase_totals["store"],
    )


if __name__ == "__main__":
    main()
