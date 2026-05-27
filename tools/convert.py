"""Convert source documents into text/ AI-friendly markdown.

Updates store/db.sqlite with convert_status, hashes, paths, error info.
Skips status=ignored/archived. Re-converts when convert_fingerprint changes
(source_hash + converter_version + convert_options + md_mode + parquet +
data_pipeline.version) or processed file missing.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import click

from services import metadata as md
from services.config import load_config
from services.text_pipeline import ConversionError, convert as do_convert
from services.text_pipeline import convert_metadata_only
from services.data_pipeline import DataPipelineError, build_parquet_artifacts
from services.hashing import fingerprint, sha256_text
from services.database import Database
from services.paths import decide_md_mode, processed_path_for, wants_parquet

from tools._common import acquire_run_lock_or_exit, setup_logger


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _unlink_parquet_files(cfg, rel_paths: list[str], log) -> None:
    """Best-effort cleanup of stale parquet artifacts from a previous run."""
    for rel in rel_paths:
        p = (cfg.knowledge_base_root / rel).resolve()
        try:
            if p.is_file():
                p.unlink()
        except OSError as e:
            log.warning("could not delete stale parquet %s: %s", p, e)


@click.command()
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
@click.option("--kb-root", "kb_root", type=str, default=None)
@click.option("--force", is_flag=True, help="Re-convert even if fingerprint matches.")
@click.option("--only", multiple=True, help="Limit to specific doc ids.")
def main(config_path: Path | None, kb_root: str | None, force: bool,
         only: tuple[str, ...]) -> None:
    cfg = load_config(config_path=config_path, knowledge_base_root_override=kb_root)
    cfg.ensure_dirs()
    log = setup_logger("convert", cfg.logs_dir / "convert.log")
    log.info("convert start: csv=%s sqlite=%s", cfg.docs_data, cfg.db_data)

    with acquire_run_lock_or_exit(cfg, step="convert", logger=log):
        _run_convert(cfg, log, force=force, only=only)


def _run_convert(cfg, log, *, force: bool, only: tuple[str, ...]) -> None:
    rows = md.load_csv(cfg.docs_data)
    store = Database(cfg.db_data)

    converter_version = cfg.text_pipeline.version
    convert_options_str = json.dumps(cfg.text_pipeline.options or {}, sort_keys=True)
    data_pipeline_version = cfg.data_pipeline.version
    data_suffixes = frozenset(s.lower() for s in (cfg.scan.data_suffixes or ()))

    n_ok = n_skipped = n_failed = 0
    only_set = set(only) if only else None

    for row in rows:
        if only_set is not None and row.id not in only_set:
            continue
        if row.status in {"ignored", "archived", "missing"}:
            log.info("SKIP %s id=%s status=%s", row.source_path, row.id, row.status)
            n_skipped += 1
            continue

        src_abs = cfg.docs_dir / row.source_path
        if not src_abs.exists():
            log.warning("MISS source file gone: %s", src_abs)
            store.update(
                row.id, row.source_path,
                convert_status="failed",
                convert_error="source file missing",
                converted_at=_now(),
            )
            n_failed += 1
            continue

        md_mode = decide_md_mode(row.source_path, row.tags, data_suffixes)
        parquet_flag = wants_parquet(row.source_path, row.tags, data_suffixes)
        # Re-run convert when md_mode or parquet flag flips even if source is
        # unchanged: bake them into the fingerprint alongside the existing
        # source_hash / converter_version / options inputs.
        fp = fingerprint(
            row.source_hash or "",
            converter_version,
            convert_options_str,
            f"md_mode={md_mode}",
            f"parquet={int(parquet_flag)}",
            f"data_pv={data_pipeline_version}",
        )
        existing = store.get(row.id)
        target = processed_path_for(cfg, row.source_path, ".md")  # default; may change

        if (
            not force
            and existing
            and existing.convert_status == "ok"
            and existing.convert_fingerprint == fp
            and existing.processed_path
            and (cfg.knowledge_base_root / existing.processed_path).exists()
        ):
            log.info("OK   (cached) %s id=%s mode=%s", row.source_path, row.id, md_mode)
            n_skipped += 1
            continue

        try:
            if md_mode == "full":
                result = do_convert(src_abs, cfg.text_pipeline.options or {})
            else:
                result = convert_metadata_only(
                    src_abs,
                    source_rel=row.source_path,
                    title=row.title,
                    doc_type=row.type,
                    tags=row.tags,
                    confidentiality=row.confidentiality,
                    status=row.status,
                    notes=row.notes,
                    source_size=row.source_size,
                    source_mtime=row.source_mtime,
                    sample_rows=cfg.data_pipeline.sample_rows,
                )
        except ConversionError as e:
            log.error("FAIL %s id=%s err=%s", row.source_path, row.id, e)
            store.update(
                row.id, row.source_path,
                source_hash=row.source_hash,
                converter_version=converter_version,
                convert_fingerprint=fp,
                convert_status="failed",
                convert_error=str(e),
                converted_at=_now(),
            )
            n_failed += 1
            continue

        target = processed_path_for(cfg, row.source_path, result.target_suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(result.text, encoding="utf-8")
        processed_hash = sha256_text(result.text)
        rel_target = target.resolve().relative_to(cfg.knowledge_base_root.resolve())

        # Data lane: regardless of whether parquet is now wanted, first clear
        # any stale registrations/files for this doc so we never leak files.
        stale = store.delete_data_tables_for_doc(row.id)
        _unlink_parquet_files(cfg, stale, log)

        if parquet_flag:
            try:
                artifacts = build_parquet_artifacts(
                    src_abs,
                    doc_id=row.id,
                    source_rel=row.source_path,
                    data_dir=cfg.data_dir,
                )
            except DataPipelineError as e:
                log.warning("PARQUET skip %s id=%s reason=%s",
                            row.source_path, row.id, e)
            else:
                kb_root_resolved = cfg.knowledge_base_root.resolve()
                for art in artifacts:
                    rel_parquet = art.parquet_path.resolve().relative_to(kb_root_resolved)
                    store.upsert_data_table(
                        doc_id=row.id,
                        source_path=row.source_path,
                        table_name=art.table_name,
                        sheet=art.sheet,
                        parquet_path=str(rel_parquet),
                        columns_json=json.dumps(art.columns, ensure_ascii=False),
                        row_count=art.row_count,
                        pipeline_version=data_pipeline_version,
                        created_at=_now(),
                    )
                log.info("PARQUET %s -> %d table(s)", row.source_path, len(artifacts))

        store.update(
            row.id, row.source_path,
            source_hash=row.source_hash,
            processed_path=str(rel_target),
            processed_hash=processed_hash,
            converter_version=converter_version,
            convert_fingerprint=fp,
            convert_status="ok",
            convert_error=None,
            converted_at=_now(),
            # invalidate downstream indexes; ingest.py will refresh them
            fulltext_index_status="pending",
            vector_index_status="pending",
        )
        n_ok += 1
        log.info("OK   %s -> %s id=%s mode=%s", row.source_path, rel_target, row.id, md_mode)

    log.info("convert done: ok=%d skipped=%d failed=%d", n_ok, n_skipped, n_failed)


if __name__ == "__main__":
    main()
