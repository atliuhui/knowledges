"""Local CLI metadata maintenance debug entry."""

from __future__ import annotations

import json
from pathlib import Path

import click

from services import metadata as md
from services.config import load_config
from services.metadata_editing import (
    BulkOperation,
    apply_update,
    bulk_apply,
    bulk_preview,
    preview_update,
)


@click.group()
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
@click.option("--kb-root", "kb_root", type=str, default=None)
@click.pass_context
def main(ctx: click.Context, config_path: Path | None, kb_root: str | None) -> None:
    """Knowledge base metadata maintenance commands."""
    ctx.ensure_object(dict)
    ctx.obj["cfg"] = load_config(config_path=config_path, knowledge_base_root_override=kb_root)


@main.command("list")
@click.option("--status", multiple=True)
@click.option("--missing-tags", is_flag=True)
@click.option("--missing-title", is_flag=True)
@click.option("--limit", type=int, default=50)
@click.pass_context
def list_docs(ctx: click.Context, status: tuple[str, ...],
              missing_tags: bool, missing_title: bool, limit: int) -> None:
    cfg = ctx.obj["cfg"]
    rows = md.load_csv(cfg.docs_data)
    if status:
        rows = [r for r in rows if r.status in set(status)]
    if missing_tags:
        rows = [r for r in rows if not r.tag_list()]
    if missing_title:
        rows = [r for r in rows if not r.title]
    docs = [
        {
            "id": r.id, "title": r.title, "source_path": r.source_path,
            "type": r.type, "tags": r.tag_list(),
            "confidentiality": r.confidentiality, "status": r.status, "notes": r.notes,
        }
        for r in rows[:limit]
    ]
    click.echo(json.dumps({"documents": docs}, ensure_ascii=False, indent=2))


@main.command("tags")
@click.pass_context
def list_tags(ctx: click.Context) -> None:
    cfg = ctx.obj["cfg"]
    rows = md.load_csv(cfg.docs_data)
    counts: dict[str, int] = {}
    for r in rows:
        for t in r.tag_list():
            counts[t] = counts.get(t, 0) + 1
    tags = [{"tag": t, "count": c} for t, c in sorted(counts.items(), key=lambda x: -x[1])]
    click.echo(json.dumps({"tags": tags}, ensure_ascii=False, indent=2))


@main.command("preview")
@click.argument("doc_id")
@click.option("--patch", required=True, help="JSON patch object.")
@click.pass_context
def preview(ctx: click.Context, doc_id: str, patch: str) -> None:
    cfg = ctx.obj["cfg"]
    patch_obj = json.loads(patch)
    result = preview_update(cfg, doc_id, patch_obj)
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


@main.command("apply")
@click.argument("doc_id")
@click.option("--patch", required=True, help="JSON patch object.")
@click.pass_context
def apply(ctx: click.Context, doc_id: str, patch: str) -> None:
    cfg = ctx.obj["cfg"]
    patch_obj = json.loads(patch)
    result = apply_update(cfg, doc_id, patch_obj)
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


@main.command("bulk-preview")
@click.option("--ids", required=True, help="Comma-separated doc ids.")
@click.option("--op", required=True, help="JSON BulkOperation.")
@click.pass_context
def bulk_preview_cmd(ctx: click.Context, ids: str, op: str) -> None:
    cfg = ctx.obj["cfg"]
    op_obj = BulkOperation(**json.loads(op))
    result = bulk_preview(cfg, [i.strip() for i in ids.split(",") if i.strip()], op_obj)
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


@main.command("bulk-apply")
@click.option("--ids", required=True)
@click.option("--op", required=True)
@click.pass_context
def bulk_apply_cmd(ctx: click.Context, ids: str, op: str) -> None:
    cfg = ctx.obj["cfg"]
    op_obj = BulkOperation(**json.loads(op))
    result = bulk_apply(cfg, [i.strip() for i in ids.split(",") if i.strip()], op_obj)
    click.echo(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
