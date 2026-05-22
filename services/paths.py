"""Path helpers and safety checks for the knowledge base layout."""

from __future__ import annotations

from pathlib import Path

from .config import Config


SUPPORTED_DOC_SUFFIXES = {
    ".md", ".markdown", ".txt", ".rst",
    ".docx", ".pdf", ".pptx",
    ".xlsx", ".csv", ".tsv",
    ".yaml", ".yml", ".json",
}


def iter_documents(cfg: Config) -> list[Path]:
    """List all candidate source files under documents/ (excluding documents.csv)."""
    docs_root = cfg.documents_dir
    if not docs_root.exists():
        return []
    skip = {cfg.documents_data.resolve()}
    result: list[Path] = []
    for p in docs_root.rglob("*"):
        if not p.is_file():
            continue
        if p.resolve() in skip:
            continue
        if p.suffix.lower() not in SUPPORTED_DOC_SUFFIXES:
            continue
        result.append(p)
    return sorted(result)


def relative_source_path(cfg: Config, file: Path) -> str:
    """Return a relative path string like 'project-a\\proposal.docx'."""
    rel = file.resolve().relative_to(cfg.documents_dir.resolve())
    return str(rel)


def ensure_inside_documents(cfg: Config, file: Path) -> None:
    """Raise if `file` escapes the documents directory (defensive path-traversal guard)."""
    try:
        file.resolve().relative_to(cfg.documents_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"path escapes documents root: {file}") from exc


def processed_path_for(cfg: Config, source_rel: str, new_suffix: str) -> Path:
    """Mirror the documents/ tree under processed/, replacing the suffix."""
    src = Path(source_rel)
    target = cfg.processed_dir / src.with_suffix(new_suffix)
    return target
