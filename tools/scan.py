"""Scan docs/ and update store/docs.csv.

Responsibilities (see README §"mcp_server\\tools\\scan.py"):
  - Compute snapshot fields (source_hash/size/mtime).
  - Add new files, update existing, mark missing as status=missing.
  - Preserve human-maintained fields.
  - Validate ids, paths, statuses, confidentiality values.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import click

from services import metadata as md
from services.config import load_config
from services.hashing import sha256_file
from services.paths import iter_documents, relative_source_path

from tools._common import acquire_run_lock_or_exit, setup_logger


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@click.command()
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
@click.option("--kb-root", "kb_root", type=str, default=None,
              help="Override knowledge_base_root.")
@click.option("--dry-run", is_flag=True, help="Do not write docs.csv.")
def main(config_path: Path | None, kb_root: str | None, dry_run: bool) -> None:
    cfg = load_config(config_path=config_path, knowledge_base_root_override=kb_root)
    cfg.ensure_dirs()
    log = setup_logger("scan", cfg.logs_dir / "scan.log")
    log.info("scan start: docs=%s csv=%s", cfg.docs_dir, cfg.docs_data)

    with acquire_run_lock_or_exit(cfg, step="scan", logger=log):
        _run_scan(cfg, log, dry_run=dry_run)


def _run_scan(cfg, log, *, dry_run: bool) -> None:
    rows = md.load_csv(cfg.docs_data)
    by_path: dict[str, md.MetadataRow] = {r.source_path: r for r in rows}

    files = iter_documents(cfg)
    n_new = n_updated = n_unchanged = n_missing = 0
    seen_paths: set[str] = set()

    for f in files:
        rel = relative_source_path(cfg, f)
        seen_paths.add(rel)
        stat = f.stat()
        sha = sha256_file(f)
        size = str(stat.st_size)
        mtime = _iso(stat.st_mtime)

        existing = by_path.get(rel)
        if existing is None:
            new_id = md.next_id(rows)
            row = md.MetadataRow(
                id=new_id,
                source_path=rel,
                source_hash=sha,
                source_size=size,
                source_mtime=mtime,
                title="",
                type="",
                tags="",
                confidentiality="",
                status="active",
                discovered_at=_now(),
                scanned_at=_now(),
                notes="",
            )
            rows.append(row)
            by_path[rel] = row
            n_new += 1
            log.info("NEW  %s id=%s", rel, new_id)
        else:
            changed = (
                existing.source_hash != sha
                or existing.source_size != size
                or existing.source_mtime != mtime
                or existing.status == "missing"
            )
            existing.source_hash = sha
            existing.source_size = size
            existing.source_mtime = mtime
            existing.scanned_at = _now()
            if existing.status == "missing":
                existing.status = "active"
            if changed:
                n_updated += 1
                log.info("UPD  %s id=%s", rel, existing.id)
            else:
                n_unchanged += 1

    # Mark missing files
    for row in rows:
        if row.source_path not in seen_paths and row.status not in {"ignored", "archived"}:
            if row.status != "missing":
                log.info("MISS %s id=%s", row.source_path, row.id)
                row.status = "missing"
                n_missing += 1

    errors = md.validate(rows)
    for e in errors:
        log.error("validation: %s", e)

    if not dry_run:
        md.save_csv(cfg.docs_data, rows)
        log.info("docs.csv saved: %s", cfg.docs_data)
    else:
        log.info("dry-run: docs.csv NOT written")

    log.info(
        "scan done: new=%d updated=%d unchanged=%d missing=%d errors=%d",
        n_new, n_updated, n_unchanged, n_missing, len(errors),
    )


if __name__ == "__main__":
    main()
