"""Local CLI debug entry for the data lane (Parquet + DuckDB).

Mirrors the MCP tools ``kb_list_data_tables`` / ``kb_read_data_table`` /
``kb_query_data`` so the data tables registered by :mod:`services.data_pipeline`
can be inspected without starting the MCP server.

Examples (PowerShell):

    python -m tools.data list
    python -m tools.data list --doc-id <id>
    python -m tools.data read <table_name> --limit 20
    python -m tools.data query "SELECT table_name, COUNT(*) FROM <t> GROUP BY 1"
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from services.config import load_config
from services.data_query import DataQueryError, read_table_rows, run_query
from services.database import Database


def _emit(payload: dict) -> None:
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


@click.group()
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
@click.option("--kb-root", "kb_root", type=str, default=None)
@click.pass_context
def main(ctx: click.Context, config_path: Path | None, kb_root: str | None) -> None:
    """Knowledge base data-lane maintenance commands."""
    ctx.ensure_object(dict)
    ctx.obj["cfg"] = load_config(config_path=config_path, knowledge_base_root_override=kb_root)


@main.command("list")
@click.option("--doc-id", "doc_id", default=None, help="Filter by source document id.")
@click.pass_context
def list_tables(ctx: click.Context, doc_id: str | None) -> None:
    """List Parquet-backed data tables registered in the knowledge base."""
    cfg = ctx.obj["cfg"]
    store = Database(cfg.db_data)
    rows = store.list_data_tables(doc_id=doc_id)
    out: list[dict] = []
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
    _emit({"data_tables": out, "count": len(out)})


@main.command("read")
@click.argument("table_name")
@click.option("--limit", type=int, default=50, show_default=True)
@click.option("--offset", type=int, default=0, show_default=True)
@click.pass_context
def read_table(ctx: click.Context, table_name: str, limit: int, offset: int) -> None:
    """Read a page of rows from a registered data table."""
    if limit <= 0 or limit > 1000:
        raise click.BadParameter("limit must be in 1..1000")
    if offset < 0:
        raise click.BadParameter("offset must be >= 0")
    cfg = ctx.obj["cfg"]
    store = Database(cfg.db_data)
    row = store.get_data_table(table_name)
    if not row:
        raise click.ClickException(f"data table not found: {table_name}")
    _emit(read_table_rows(cfg.knowledge_base_root, row, limit=limit, offset=offset))


@main.command("query")
@click.argument("sql")
@click.option("--limit", type=int, default=1000, show_default=True,
              help="Hard LIMIT cap applied when the SQL contains no LIMIT clause.")
@click.pass_context
def query(ctx: click.Context, sql: str, limit: int) -> None:
    """Run a read-only SELECT/WITH SQL query over registered data tables."""
    if limit <= 0 or limit > 10000:
        raise click.BadParameter("limit must be in 1..10000")
    cfg = ctx.obj["cfg"]
    store = Database(cfg.db_data)
    rows = store.list_data_tables()
    try:
        _emit(run_query(cfg.knowledge_base_root, rows, sql, hard_cap=limit))
    except DataQueryError as e:
        _emit({"error": str(e), "type": "DataQueryError"})
        raise SystemExit(1)


if __name__ == "__main__":
    main()
