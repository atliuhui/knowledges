"""Local CLI search debug entry. Returns JSON results."""

from __future__ import annotations

import json
from pathlib import Path

import click

from services.config import load_config
from services.hybrid_search import search


@click.command()
@click.argument("query")
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=None)
@click.option("--kb-root", "kb_root", type=str, default=None)
@click.option("--mode", type=click.Choice(["hybrid", "fulltext", "vector"]),
              default=None, help="Override default search mode.")
@click.option("--limit", type=int, default=10)
def main(query: str, config_path: Path | None, kb_root: str | None,
         mode: str | None, limit: int) -> None:
    cfg = load_config(config_path=config_path, knowledge_base_root_override=kb_root)
    cfg.ensure_dirs()
    effective_mode = mode or cfg.mcp.default_search_mode
    hits = search(cfg, query=query, mode=effective_mode, limit=limit)
    click.echo(json.dumps({"query": query, "mode": effective_mode, "hits": hits},
                          ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
