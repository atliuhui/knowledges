"""Build / update Tantivy full-text index and LanceDB vector index from processed/."""

from __future__ import annotations

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


def _load_embedder(model_name: str):  # type: ignore[no-untyped-def]
    from sentence_transformers import SentenceTransformer  # type: ignore
    return SentenceTransformer(model_name)


@click.command()
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
@click.option("--kb-root", "kb_root", type=str, default=None)
@click.option("--force", is_flag=True, help="Re-index all converted docs.")
@click.option("--only", multiple=True, help="Limit to specific doc ids.")
@click.option("--no-vector", is_flag=True, help="Skip vector index (useful if no GPU/model).")
def main(config_path: Path | None, kb_root: str | None, force: bool,
         only: tuple[str, ...], no_vector: bool) -> None:
    cfg = load_config(config_path=config_path, knowledge_base_root_override=kb_root)
    cfg.ensure_dirs()
    log = setup_logger("ingest", cfg.logs_dir / "ingest.log")
    log.info("ingest start: fulltext=%s vector=%s", cfg.fulltext_index_dir, cfg.vector_index_dir)

    with acquire_run_lock_or_exit(cfg, step="ingest", logger=log):
        _run_ingest(cfg, log, force=force, only=only, no_vector=no_vector)


def _run_ingest(cfg, log, *, force: bool, only: tuple[str, ...], no_vector: bool) -> None:
    rows = md.load_csv(cfg.documents_data)
    csv_by_id = {r.id: r for r in rows}
    store = Database(cfg.database_data)
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
            embedder = _load_embedder(cfg.embedding.model)
        except Exception as e:  # noqa: BLE001
            log.warning("vector backend unavailable, continuing fulltext only: %s", e)
            vec = None
            embedder = None

    n_ok = n_skipped = n_failed = 0

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

        processed_abs = cfg.knowledge_base_root / rec.processed_path
        if not processed_abs.exists():
            log.error("FAIL processed missing: %s", processed_abs)
            store.update(
                rec.id, rec.source_path,
                fulltext_index_status="failed",
                fulltext_index_error="processed file missing",
                fulltext_indexed_at=_now(),
            )
            n_failed += 1
            continue

        text = processed_abs.read_text(encoding="utf-8", errors="replace")
        chunks = chunk_text(
            text,
            max_chars=cfg.chunking.max_chars,
            overlap_chars=cfg.chunking.overlap_chars,
        )

        tags = row.tag_list()
        common_meta = {
            "title": row.title or row.source_path,
            "tags": tags,
            "type": row.type,
            "confidentiality": row.confidentiality,
            "source_path": row.source_path,
            "processed_path": rec.processed_path,
        }

        # Full-text
        ft_payload = [
            {**common_meta, "chunk_id": c.chunk_id, "text": c.text}
            for c in chunks
        ]
        try:
            if needs_ft:
                ft.add_chunks(rec.id, ft_payload)
                store.update(
                    rec.id, rec.source_path,
                    chunking_version=cfg.chunking.version,
                    fulltext_engine=cfg.fulltext_engine,
                    fulltext_index_version=FT_VERSION,
                    fulltext_index_status="ok",
                    fulltext_index_error=None,
                    fulltext_indexed_at=_now(),
                )
        except Exception as e:  # noqa: BLE001
            log.error("FT  FAIL %s: %s", rec.id, e)
            store.update(
                rec.id, rec.source_path,
                fulltext_index_status="failed",
                fulltext_index_error=str(e),
                fulltext_indexed_at=_now(),
            )
            n_failed += 1
            continue

        # Vector
        if needs_vec and vec is not None and embedder is not None:
            try:
                texts = [c.text for c in chunks]
                if not texts:
                    embeddings = []
                else:
                    # Documents are encoded without the query prompt; that prompt is
                    # only used at query time (see services.hybrid_search).
                    embeddings = embedder.encode(
                        texts,
                        normalize_embeddings=cfg.embedding.normalize,
                    )
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
                vec.add_chunks(rec.id, vec_payload)
                store.update(
                    rec.id, rec.source_path,
                    vector_engine=cfg.vector_engine,
                    embedding_model=cfg.embedding.model,
                    vector_index_version="vector-v1",
                    vector_index_status="ok",
                    vector_index_error=None,
                    vector_indexed_at=_now(),
                )
            except Exception as e:  # noqa: BLE001
                log.error("VEC FAIL %s: %s", rec.id, e)
                store.update(
                    rec.id, rec.source_path,
                    vector_index_status="failed",
                    vector_index_error=str(e),
                    vector_indexed_at=_now(),
                )
                n_failed += 1
                continue

        n_ok += 1
        log.info("OK   id=%s chunks=%d", rec.id, len(chunks))

    log.info("ingest done: ok=%d skipped=%d failed=%d", n_ok, n_skipped, n_failed)


if __name__ == "__main__":
    main()
