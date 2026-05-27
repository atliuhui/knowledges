"""Shared base names for the text pipeline.

Holds the small, dependency-free primitives that both the dispatcher
(:mod:`services.text_pipeline`) and the per-format submodules
(:mod:`services.image_ocr`, :mod:`services.audio_asr`,
:mod:`services.metadata_preview`) need.

This module exists to break the import cycle that would otherwise form when
``python -m services.text_pipeline`` runs the dispatcher as ``__main__`` and a
submodule tries to re-import it via ``from .text_pipeline import ...``.
Everything here is intentionally minimal — no heavy third-party imports at
module load (MarkItDown is loaded lazily inside :func:`_get_markitdown`).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class ConversionResult:
    text: str
    target_suffix: str  # always ".md"


class ConversionError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# MarkItDown
# ---------------------------------------------------------------------------

_MARKITDOWN_INSTANCE: Any = None


def _get_markitdown() -> Any:
    """Lazily build and cache a single offline MarkItDown instance."""
    global _MARKITDOWN_INSTANCE
    if _MARKITDOWN_INSTANCE is not None:
        return _MARKITDOWN_INSTANCE
    try:
        from markitdown import MarkItDown  # type: ignore
    except ImportError as e:
        raise ConversionError(
            "markitdown not installed: reinstall the project with `pip install -e .`"
        ) from e
    # Offline-only: do not pass llm_client / docintel_endpoint / cu_endpoint,
    # and keep plugins disabled so no third party can introduce network I/O.
    _MARKITDOWN_INSTANCE = MarkItDown(enable_plugins=False)
    return _MARKITDOWN_INSTANCE


# File extensions whose MarkItDown converter reads raw text bytes and therefore
# needs an explicit charset hint. Without it, MarkItDown's chardet step can
# return None for short / pure-ASCII-prefix files and fall back to the system
# default codec (ascii on Windows), crashing on UTF-8 multibyte sequences.
_TEXTISH_MARKITDOWN_SUFFIXES: frozenset[str] = frozenset({
    ".md", ".markdown", ".txt", ".rst",
    ".yaml", ".yml",
    ".json", ".xml",
    ".html", ".htm",
    ".csv", ".tsv",
})


def _stream_info_for(path: Path) -> Any:
    """Return a MarkItDown ``StreamInfo`` carrying an explicit UTF-8 charset for
    text-ish formats, or ``None`` for binary formats where the converter ignores
    charset (docx / pdf / pptx / xlsx / images / ...).
    """
    suffix = path.suffix.lower()
    if suffix not in _TEXTISH_MARKITDOWN_SUFFIXES:
        return None
    try:
        from markitdown import StreamInfo  # type: ignore
    except ImportError:
        return None
    return StreamInfo(
        extension=suffix,
        charset="utf-8",
        filename=path.name,
        local_path=str(path),
    )


def _markitdown_text(path: Path) -> str:
    md = _get_markitdown()
    stream_info = _stream_info_for(path)
    kwargs: dict[str, Any] = {}
    if stream_info is not None:
        kwargs["stream_info"] = stream_info
    try:
        # convert_local() restricts I/O to a local filesystem path; MarkItDown
        # never fetches URIs or opens arbitrary streams on our behalf.
        # For text-ish formats we pass StreamInfo(charset="utf-8") so MarkItDown's
        # PlainTextConverter / HtmlConverter / XmlConverter / CsvConverter skip
        # chardet (which on Windows can fall back to ascii / cp936 and crash on
        # UTF-8 Chinese bytes with UnicodeDecodeError).
        result = md.convert_local(str(path), **kwargs)
    except Exception as e:  # noqa: BLE001
        raise ConversionError(f"markitdown failed on {path.name}: {e}") from e
    for attr in ("markdown", "text_content"):
        value = getattr(result, attr, None)
        if isinstance(value, str) and value:
            return value
    raise ConversionError("markitdown returned no markdown content")


# ---------------------------------------------------------------------------
# Shared normalization helpers
# ---------------------------------------------------------------------------


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.rstrip("\n") + "\n"
