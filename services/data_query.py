"""Read-only DuckDB query helpers over the registered Parquet data tables.

Each registered row in ``data_tables`` is exposed as a DuckDB view named after
``table_name`` and backed by ``read_parquet(<parquet_path>)``. Queries are
restricted to read-only SQL (single SELECT/WITH statement) and an automatic
LIMIT cap is enforced when none is supplied by the user.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    import duckdb  # type: ignore
except ImportError as e:  # pragma: no cover
    raise RuntimeError(
        "duckdb is required for data-lane MCP tools. Install with: pip install duckdb"
    ) from e


class DataQueryError(RuntimeError):
    """Raised for invalid / unsafe / failed data-lane queries."""


# Statements/keywords that mutate state or escape the read-only sandbox.
_FORBIDDEN_KEYWORDS = (
    "insert", "update", "delete", "drop", "create", "alter", "attach", "detach",
    "copy", "export", "import", "pragma", "set", "load", "install", "vacuum",
    "checkpoint", "truncate", "call", "merge",
)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class DataTableRef:
    table_name: str
    parquet_abs_path: Path


def _strip_sql_comments(sql: str) -> str:
    # Strip /* ... */ then -- to end-of-line.
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def _validate_select(sql: str) -> str:
    """Return the cleaned-up SELECT/WITH SQL or raise DataQueryError.

    Only one statement is permitted; mutation/extension keywords are rejected.
    """
    stripped = _strip_sql_comments(sql).strip().rstrip(";").strip()
    if not stripped:
        raise DataQueryError("empty SQL")
    if ";" in stripped:
        raise DataQueryError("only a single SQL statement is allowed")
    lowered = stripped.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise DataQueryError("only SELECT or WITH queries are allowed")
    # Word-boundary keyword scan to avoid false positives like `dropdown`.
    tokens = set(re.findall(r"[A-Za-z_]+", lowered))
    bad = tokens.intersection(_FORBIDDEN_KEYWORDS)
    if bad:
        raise DataQueryError(f"forbidden keyword(s): {sorted(bad)}")
    return stripped


def _apply_limit_cap(sql: str, hard_cap: int) -> tuple[str, bool]:
    """Wrap query in an outer LIMIT when the user did not specify one.

    Returns ``(sql, capped)``. We avoid editing the user's own LIMIT clause and
    instead wrap as a subquery; this is robust for both SELECT and WITH forms.
    """
    if re.search(r"\blimit\b", sql, flags=re.IGNORECASE):
        return sql, False
    return f"SELECT * FROM ({sql}) AS _kb_q LIMIT {hard_cap}", True


def _quote_ident(name: str) -> str:
    if not _IDENT_RE.match(name):
        raise DataQueryError(f"invalid identifier: {name!r}")
    return f'"{name}"'


def _open_connection(
    kb_root: Path,
    refs: Iterable[DataTableRef],
) -> "duckdb.DuckDBPyConnection":
    """Open an in-memory DuckDB connection with one view per registered table."""
    con = duckdb.connect(database=":memory:")
    # NOTE: We deliberately do NOT call `SET enable_external_access=false`
    # because that pragma also blocks `read_parquet()` on local files, which
    # is the very operation we need here. User SQL is restricted by the
    # `_validate_select` keyword filter (no ATTACH / COPY / EXPORT / etc.).
    for ref in refs:
        p = ref.parquet_abs_path
        if not p.is_file():
            continue
        ident = _quote_ident(ref.table_name)
        path_lit = str(p.as_posix()).replace("'", "''")
        con.execute(
            f"CREATE OR REPLACE VIEW {ident} AS SELECT * FROM read_parquet('{path_lit}')"
        )
    return con


def _resolve_refs(
    rows: list[dict],
    kb_root: Path,
) -> list[DataTableRef]:
    refs: list[DataTableRef] = []
    for r in rows:
        rel = r.get("parquet_path") or ""
        if not rel:
            continue
        refs.append(
            DataTableRef(
                table_name=str(r["table_name"]),
                parquet_abs_path=(kb_root / rel).resolve(),
            )
        )
    return refs


def read_table_rows(
    kb_root: Path,
    row: dict,
    *,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """Page rows from a single registered table via DuckDB."""
    ref = _resolve_refs([row], kb_root)
    if not ref:
        raise DataQueryError(f"table has no parquet_path: {row.get('table_name')!r}")
    con = _open_connection(kb_root, ref)
    try:
        ident = _quote_ident(ref[0].table_name)
        cur = con.execute(f"SELECT * FROM {ident} LIMIT ? OFFSET ?", [limit, offset])
        columns = [d[0] for d in cur.description] if cur.description else []
        data = cur.fetchall()
    finally:
        con.close()
    columns_meta: list[str] = []
    try:
        columns_meta = json.loads(row.get("columns_json") or "[]")
    except Exception:  # noqa: BLE001
        columns_meta = []
    return {
        "table_name": row.get("table_name"),
        "doc_id": row.get("doc_id"),
        "source_path": row.get("source_path"),
        "sheet": row.get("sheet"),
        "parquet_path": row.get("parquet_path"),
        "row_count": row.get("row_count"),
        "columns": columns_meta or columns,
        "limit": limit,
        "offset": offset,
        "returned": len(data),
        "rows": [
            {col: cell for col, cell in zip(columns, record)}
            for record in data
        ],
    }


def run_query(
    kb_root: Path,
    rows: list[dict],
    sql: str,
    *,
    hard_cap: int = 1000,
) -> dict[str, Any]:
    """Run a read-only SELECT/WITH query over every registered data table."""
    cleaned = _validate_select(sql)
    capped_sql, capped = _apply_limit_cap(cleaned, hard_cap)
    refs = _resolve_refs(rows, kb_root)
    con = _open_connection(kb_root, refs)
    try:
        cur = con.execute(capped_sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        data = cur.fetchall()
    except duckdb.Error as e:  # type: ignore[attr-defined]
        raise DataQueryError(str(e)) from e
    finally:
        con.close()
    return {
        "ok": True,
        "sql_executed": capped_sql,
        "limit_cap_applied": capped,
        "hard_cap": hard_cap,
        "available_tables": [r.table_name for r in refs],
        "columns": columns,
        "row_count": len(data),
        "rows": [
            {col: cell for col, cell in zip(columns, record)}
            for record in data
        ],
    }
