"""Path helpers and safety checks for the knowledge base layout."""

from __future__ import annotations

from pathlib import Path

from .config import Config


# Audio / video formats handled by the audio_asr branch of the text pipeline.
# Kept here (rather than in services.audio_asr) so that services.paths has no
# dependency on the text-pipeline submodules and the import graph stays acyclic.
AUDIO_SUFFIXES: frozenset[str] = frozenset({
    ".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".amr",
})
VIDEO_SUFFIXES: frozenset[str] = frozenset({
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".flv", ".m4v", ".wmv",
    ".ts", ".mpg", ".mpeg",
})
MEDIA_SUFFIXES: frozenset[str] = AUDIO_SUFFIXES | VIDEO_SUFFIXES


# Data-class formats: by default these only produce a metadata-only Markdown
# preview during convert (no full content), and are eligible for Parquet
# export when the human author tags the document with ``data``. Anything not
# in this set is treated as "text-class". The runtime default lives here; the
# effective set can be overridden via ``config.yaml -> scan.data_suffixes``
# and is threaded in by callers (see ``tools/convert.py``).
DATA_LIKE_SUFFIXES: frozenset[str] = frozenset({
    ".csv", ".tsv", ".xlsx", ".xls",
    ".json", ".xml", ".yaml", ".yml",
})


SUPPORTED_DOC_SUFFIXES = {
    # Plain text / markup
    ".md", ".markdown", ".txt", ".rst",
    ".yaml", ".yml", ".json", ".xml",
    ".html", ".htm",
    # Office documents
    ".docx", ".pdf", ".pptx",
    ".xlsx", ".xls",
    ".csv", ".tsv",
    # Diagrams / mind maps (offline structural extraction)
    ".drawio", ".xmind",
    # Email / notebooks / archives / ebooks
    ".msg", ".ipynb", ".epub", ".zip",
    # Images (OCR + EXIF; .gif is metadata-only)
    ".png", ".jpg", ".jpeg", ".gif",
} | AUDIO_SUFFIXES | VIDEO_SUFFIXES


def is_data_like(source_path: str, data_suffixes: frozenset[str] | None = None) -> bool:
    """True if the source file's extension is in the data-class suffix set."""
    sfx = data_suffixes if data_suffixes is not None else DATA_LIKE_SUFFIXES
    return Path(source_path).suffix.lower() in sfx


def _split_tags(tags: str) -> set[str]:
    return {t.strip().lower() for t in (tags or "").split(";") if t.strip()}


def decide_md_mode(
    source_path: str,
    tags: str,
    data_suffixes: frozenset[str] | None = None,
) -> str:
    """Return ``"full"`` or ``"metadata"`` for the processed/ Markdown output.

    Rules (tags are case-insensitive, semicolon-separated):
    * ``text`` in tags          -> full
    * ``data`` in tags          -> metadata
    * neither tag, data-class   -> metadata
    * neither tag, text-class   -> full
    """
    tag_set = _split_tags(tags)
    if "text" in tag_set:
        return "full"
    if "data" in tag_set:
        return "metadata"
    return "metadata" if is_data_like(source_path, data_suffixes) else "full"


def wants_parquet(
    source_path: str,
    tags: str,
    data_suffixes: frozenset[str] | None = None,
) -> bool:
    """True if the file is data-class AND the author tagged it with ``data``.

    Parquet generation is opt-in: only files explicitly tagged ``data`` are
    materialized into the data lane, regardless of extension.
    """
    return is_data_like(source_path, data_suffixes) and "data" in _split_tags(tags)



def iter_documents(cfg: Config) -> list[Path]:
    """List all candidate source files under docs/ (excluding docs.csv)."""
    docs_root = cfg.docs_dir
    if not docs_root.exists():
        return []
    skip = {cfg.docs_data.resolve()}
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
    rel = file.resolve().relative_to(cfg.docs_dir.resolve())
    return str(rel)


def ensure_inside_documents(cfg: Config, file: Path) -> None:
    """Raise if `file` escapes the docs directory (defensive path-traversal guard)."""
    try:
        file.resolve().relative_to(cfg.docs_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"path escapes docs root: {file}") from exc


def processed_path_for(cfg: Config, source_rel: str, new_suffix: str) -> Path:
    """Mirror the docs/ tree under text/, replacing the suffix."""
    src = Path(source_rel)
    target = cfg.text_dir / src.with_suffix(new_suffix)
    return target
