"""Convert source documents into processed/ AI-friendly text.

Updates index/database.sqlite with convert_status, hashes, paths, error info.
Skips status=ignored/archived. Re-converts when convert_fingerprint changes
(source_hash + converter_version + convert_options) or processed file missing.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import click

from services import metadata as md
from services.config import load_config
from services.conversion import ConversionError, convert as do_convert
from services.hashing import fingerprint, sha256_text
from services.database import Database
from services.paths import processed_path_for

from tools._common import acquire_run_lock_or_exit, setup_logger


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


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
    log.info("convert start: csv=%s sqlite=%s", cfg.documents_data, cfg.database_data)

    with acquire_run_lock_or_exit(cfg, step="convert", logger=log):
        _run_convert(cfg, log, force=force, only=only)


def _run_convert(cfg, log, *, force: bool, only: tuple[str, ...]) -> None:
    rows = md.load_csv(cfg.documents_data)
    store = Database(cfg.database_data)

    converter_version = cfg.converter.version
    convert_options_str = json.dumps(cfg.converter.options or {}, sort_keys=True)

    n_ok = n_skipped = n_failed = 0
    only_set = set(only) if only else None

    for row in rows:
        if only_set is not None and row.id not in only_set:
            continue
        if row.status in {"ignored", "archived", "missing"}:
            log.info("SKIP %s id=%s status=%s", row.source_path, row.id, row.status)
            n_skipped += 1
            continue

        src_abs = cfg.documents_dir / row.source_path
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

        fp = fingerprint(row.source_hash or "", converter_version, convert_options_str)
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
            log.info("OK   (cached) %s id=%s", row.source_path, row.id)
            n_skipped += 1
            continue

        try:
            result = do_convert(src_abs)
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
        log.info("OK   %s -> %s id=%s", row.source_path, rel_target, row.id)

    log.info("convert done: ok=%d skipped=%d failed=%d", n_ok, n_skipped, n_failed)


if __name__ == "__main__":
    main()
