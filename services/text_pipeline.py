"""Text-pipeline dispatcher.

This module is the single entry point for converting any local source file to
Markdown. It owns:

* The shared types (:class:`ConversionResult`, :class:`ConversionError`,
  :data:`REPO_ROOT`).
* The MarkItDown helpers used by most formats and by image / audio branches
  for their ``## Metadata`` section.
* The Draw.io and XMind offline parsers (small, format-specific branches).
* The :func:`convert` routing table and the :func:`main` CLI for preloading.

The heavy subsystems live in dedicated sibling modules — they are imported at
the *bottom* of this file (after the shared base names are defined) so the
circular dependency between dispatcher and submodules resolves cleanly:

* :mod:`services.image_ocr`        — RapidOCR for ``.png`` / ``.jpg`` / ``.jpeg``.
* :mod:`services.audio_asr`        — sherpa-onnx + SenseVoice + ffmpeg.
* :mod:`services.metadata_preview` — metadata-only Markdown for data-class files.

Offline-only design:

* MarkItDown is invoked through ``convert_local()`` with no ``llm_client`` /
  ``docintel_endpoint`` / ``cu_endpoint`` and plugins disabled. The online
  extras (``audio-transcription``, ``youtube-transcription``, Azure
  Document Intelligence, Azure Content Understanding) are intentionally
  not installed; see pyproject.toml.
* Images (.png / .jpg / .jpeg) are NOT delegated to MarkItDown for text
  extraction. Instead :mod:`services.image_ocr` runs RapidOCR locally
  (ONNX runtime, fully offline), then appends MarkItDown's EXIF/metadata
  block so downstream search can also match on camera / file metadata.
* Audio and video files are normalized to 16 kHz mono PCM via ffmpeg
  (system binary on PATH, falling back to imageio-ffmpeg's bundled binary)
  and transcribed offline by :mod:`services.audio_asr` with sherpa-onnx +
  SenseVoice-Small. Audio metadata from MarkItDown is appended when
  available.

See:
* https://github.com/microsoft/markitdown
* https://github.com/RapidAI/RapidOCR
* https://github.com/k2-fsa/sherpa-onnx
* https://github.com/FunAudioLLM/SenseVoiceSmall
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._text_base import (
    REPO_ROOT,
    ConversionError,
    ConversionResult,
    _get_markitdown,
    _markitdown_text,
    _normalize_text,
    _stream_info_for,
    _TEXTISH_MARKITDOWN_SUFFIXES,
)


# ---------------------------------------------------------------------------
# Draw.io / XMind (offline structural parsers, no MarkItDown)
# ---------------------------------------------------------------------------

_DRAWIO_SUFFIXES: frozenset[str] = frozenset({".drawio"})
_XMIND_SUFFIXES: frozenset[str] = frozenset({".xmind"})


def _convert_drawio(path: Path) -> str:
    """Extract visible text labels from a Draw.io diagram file.

    Supports both uncompressed (<diagram><mxGraphModel>...) and compressed
    (deflate + base64 + url-encoded inner XML) layouts.
    """
    import base64
    import html
    import re
    import zlib
    from urllib.parse import unquote
    from xml.etree import ElementTree as ET

    def _strip_html(text: str) -> str:
        text = html.unescape(text)
        text = text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
        if "<" not in text:
            return text
        return re.sub(r"<[^>]+>", "", text)

    def _diagram_xml(diagram_el: ET.Element) -> str:
        if len(diagram_el):
            return ET.tostring(diagram_el[0], encoding="unicode")
        raw = (diagram_el.text or "").strip()
        if not raw:
            return ""
        if raw.startswith("<"):
            return raw
        try:
            decoded = base64.b64decode(raw)
            inflated = zlib.decompress(decoded, -15)
            return unquote(inflated.decode("utf-8", errors="replace"))
        except Exception as e:  # noqa: BLE001
            raise ConversionError(f"failed to decode drawio compressed diagram: {e}") from e

    def _extract_values(diagram_xml: str) -> list[str]:
        if not diagram_xml.strip():
            return []
        try:
            root = ET.fromstring(diagram_xml)
        except Exception as e:  # noqa: BLE001
            raise ConversionError(f"failed to parse drawio diagram xml: {e}") from e
        values: list[str] = []
        for cell in root.findall(".//mxCell"):
            v = (cell.attrib.get("value") or "").strip()
            if v:
                try:
                    clean = _strip_html(v).strip()
                except Exception:  # noqa: BLE001
                    clean = html.unescape(v)
                if clean:
                    values.append(clean)
        return values

    try:
        root = ET.parse(str(path)).getroot()
    except Exception as e:  # noqa: BLE001
        raise ConversionError(f"failed to parse drawio file: {e}") from e

    parts: list[str] = [f"# {path.stem}", ""]
    diagrams = root.findall(".//diagram")
    if not diagrams and root.tag == "diagram":
        diagrams = [root]
    for idx, d in enumerate(diagrams, start=1):
        name = (d.attrib.get("name") or f"Diagram {idx}").strip()
        parts.append(f"## Diagram {idx}: {name}")
        vals = _extract_values(_diagram_xml(d))
        if vals:
            parts.extend(f"- {v}" for v in vals)
        else:
            parts.append("(no text labels)")
        parts.append("")

    if len(parts) <= 2:
        raise ConversionError("drawio file contains no diagram")
    return "\n".join(parts).rstrip() + "\n"


def _convert_xmind(path: Path) -> str:
    """Extract topic tree text from an XMind file (.xmind ZIP container).

    Supports both modern ``content.json`` and legacy ``content.xml`` layouts.
    """
    import json
    import zipfile

    def _walk_topic(topic: dict, lines: list[str], level: int = 1) -> None:
        title = str(topic.get("title") or "").strip()
        if title:
            lines.append(f"{'#' * min(level, 6)} {title}")

        notes = topic.get("notes") or {}
        if isinstance(notes, dict):
            plain = notes.get("plain")
            if isinstance(plain, dict):
                content = str(plain.get("content") or "").strip()
                if content:
                    lines.append(content)

        children = topic.get("children") or {}
        attached = children.get("attached") if isinstance(children, dict) else None
        if isinstance(attached, list):
            for child in attached:
                if isinstance(child, dict):
                    _walk_topic(child, lines, level + 1)

    if not zipfile.is_zipfile(path):
        raise ConversionError("xmind file is not a valid zip archive")

    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())
        if "content.json" in names:
            try:
                data = json.loads(zf.read("content.json").decode("utf-8", errors="replace"))
            except Exception as e:  # noqa: BLE001
                raise ConversionError(f"failed to parse xmind content.json: {e}") from e

            sheets = data if isinstance(data, list) else [data]
            lines: list[str] = [f"# {path.stem}", ""]
            for idx, sheet in enumerate(sheets, start=1):
                if not isinstance(sheet, dict):
                    continue
                title = str(sheet.get("title") or f"Sheet {idx}").strip()
                lines.append(f"## Sheet {idx}: {title}")
                root_topic = sheet.get("rootTopic")
                if isinstance(root_topic, dict):
                    _walk_topic(root_topic, lines, level=3)
                lines.append("")
            if len(lines) <= 2:
                raise ConversionError("xmind content.json has no readable sheets")
            return "\n".join(lines).rstrip() + "\n"

        if "content.xml" in names:
            from xml.etree import ElementTree as ET

            ns = {
                "xmap": "urn:xmind:xmap:xmlns:content:2.0",
                "fo": "http://www.w3.org/1999/XSL/Format",
            }

            try:
                root = ET.fromstring(zf.read("content.xml"))
            except Exception as e:  # noqa: BLE001
                raise ConversionError(f"failed to parse xmind content.xml: {e}") from e

            lines = [f"# {path.stem}", ""]
            sheets = root.findall("xmap:sheet", ns)
            for idx, sheet in enumerate(sheets, start=1):
                s_title = sheet.findtext("xmap:title", default=f"Sheet {idx}", namespaces=ns).strip()
                lines.append(f"## Sheet {idx}: {s_title}")
                for topic in sheet.findall(".//xmap:topic", ns):
                    title = topic.findtext("xmap:title", default="", namespaces=ns).strip()
                    if title:
                        lines.append(f"- {title}")
                lines.append("")

            if len(lines) <= 2:
                raise ConversionError("xmind content.xml has no readable topics")
            return "\n".join(lines).rstrip() + "\n"

    raise ConversionError("unsupported xmind structure: missing content.json/content.xml")


# ---------------------------------------------------------------------------
# Shared normalization helpers (re-exports from _text_base, kept here so the
# dispatcher's public surface is unchanged after the split).
# ---------------------------------------------------------------------------

_SPREADSHEET_SUFFIXES: frozenset[str] = frozenset({".xlsx", ".xls"})
_EMPTYISH_CELL_VALUES: frozenset[str] = frozenset({"", "nan", "none", "null", "na", "n/a"})


def _clean_spreadsheet_markdown_tables(text: str) -> str:
    """Reduce token-heavy noise from spreadsheet markdown tables.

    MarkItDown may render sparse sheets with massive `NaN`-filled rows. We map
    empty-ish placeholders to blank cells and drop rows that become entirely
    empty, while preserving table headers and separators.
    """
    lines = text.split("\n")
    out: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            out.append(line)
            continue

        # Keep markdown table separator rows untouched: | --- | --- |
        if "---" in stripped and stripped.replace("|", "").replace("-", "").strip() == "":
            out.append(line)
            continue

        cells = [c.strip() for c in stripped.strip("|").split("|")]
        cleaned_cells: list[str] = []
        non_empty_count = 0
        for cell in cells:
            if cell.lower() in _EMPTYISH_CELL_VALUES:
                cleaned_cells.append("")
            else:
                cleaned_cells.append(cell)
                non_empty_count += 1

        # Drop fully-empty data rows after normalization.
        if non_empty_count == 0:
            continue

        out.append("| " + " | ".join(cleaned_cells) + " |")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Submodule imports (placed here so submodules can ``from .text_pipeline import``
# the shared base names defined above without triggering a circular import.)
# ---------------------------------------------------------------------------

from . import audio_asr as _audio_asr  # noqa: E402
from . import image_ocr as _image_ocr  # noqa: E402
from .metadata_preview import convert_metadata_only  # noqa: E402,F401  re-export


# Back-compat re-exports for callers that imported private suffix sets from
# this module before the split (notably services.paths).
_AUDIO_SUFFIXES = _audio_asr.AUDIO_SUFFIXES
_VIDEO_SUFFIXES = _audio_asr.VIDEO_SUFFIXES
_MEDIA_SUFFIXES = _audio_asr.MEDIA_SUFFIXES
_IMAGE_OCR_SUFFIXES = _image_ocr.IMAGE_OCR_SUFFIXES


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def convert(path: Path, options: dict[str, Any] | None = None) -> ConversionResult:
    """Convert a local source file to Markdown.

    Routing:
    * Images (.png/.jpg/.jpeg) -> RapidOCR text + MarkItDown EXIF.
    * Audio / video -> ffmpeg PCM -> sherpa-onnx SenseVoice transcript +
      MarkItDown metadata (best-effort).
    * Everything else -> MarkItDown.convert_local().
    """
    opts = options or {}
    suffix = path.suffix.lower()
    if suffix in _image_ocr.IMAGE_OCR_SUFFIXES:
        text = _image_ocr.convert_image(path, opts)
    elif suffix in _audio_asr.MEDIA_SUFFIXES:
        text = _audio_asr.convert_media(path, opts)
    elif suffix in _DRAWIO_SUFFIXES:
        text = _convert_drawio(path)
    elif suffix in _XMIND_SUFFIXES:
        text = _convert_xmind(path)
    else:
        text = _markitdown_text(path)
        if suffix in _SPREADSHEET_SUFFIXES:
            text = _clean_spreadsheet_markdown_tables(text)
    return ConversionResult(_normalize_text(text), ".md")


# ---------------------------------------------------------------------------
# Preload helpers (CLI entry points for warming model caches once at install)
# ---------------------------------------------------------------------------


def _load_converter_options() -> dict[str, Any]:
    """Read ``text_pipeline.*`` from config.yaml, with safe fallback to {}."""
    try:
        from services.config import load_config  # type: ignore
    except ImportError:
        return {}
    try:
        cfg = load_config()
    except Exception:  # noqa: BLE001
        return {}
    return dict(getattr(cfg.text_pipeline, "options", {}) or {})


def preload_image_ocr(options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Force RapidOCR to download / mmap its ONNX models once.

    Reads ``text_pipeline.*`` from config.yaml when ``options`` is omitted so
    the same det/rec/model_type defaults are honored as the real convert path.
    """
    opts = options if options is not None else _load_converter_options()
    return _image_ocr.preload(opts)


def preload_audio(options: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve ffmpeg + build a sherpa-onnx OfflineRecognizer once.

    Mirrors the dispatch used by ``audio_asr.convert_media``: reads
    ``text_pipeline.*`` from config.yaml when ``options`` is omitted, so changing
    the YAML alone is enough to keep this in sync with real conversions.
    """
    opts = options if options is not None else _load_converter_options()
    return _audio_asr.preload(opts)


def main() -> int:
    """``python -m services.text_pipeline [image|audio|all]`` preload entry."""
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="services.text_pipeline",
        description="Preload converter resources (ONNX models, ffmpeg binary).",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default="all",
        choices=("image", "audio", "all"),
        help="Which subsystem to preload (default: all).",
    )
    args = parser.parse_args()

    results: dict[str, Any] = {}
    if args.target in ("image", "all"):
        try:
            results["image"] = preload_image_ocr()
        except ConversionError as e:
            results["image"] = {"ok": False, "error": str(e)}
    if args.target in ("audio", "all"):
        try:
            results["audio"] = preload_audio()
        except ConversionError as e:
            results["audio"] = {"ok": False, "error": str(e)}
    print(json.dumps(results, ensure_ascii=False, indent=2))
    ok = all(not (isinstance(v, dict) and v.get("ok") is False) for v in results.values())
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
