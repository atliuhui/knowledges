"""Document conversion: original formats -> AI-friendly processed/.

Each converter returns (text, target_suffix). Conversion is best-effort; missing
optional dependencies for binary formats degrade to a clear error rather than a crash.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class ConversionResult:
    text: str
    target_suffix: str  # e.g. ".md" or ".csv"


class ConversionError(RuntimeError):
    pass


def _convert_text(path: Path) -> ConversionResult:
    return ConversionResult(path.read_text(encoding="utf-8", errors="replace"), ".md"
                            if path.suffix.lower() in {".md", ".markdown"} else ".md")


def _convert_plain(path: Path) -> ConversionResult:
    return ConversionResult(path.read_text(encoding="utf-8", errors="replace"), ".md")


def _convert_docx(path: Path) -> ConversionResult:
    try:
        from docx import Document  # type: ignore
    except ImportError as e:
        raise ConversionError(f"python-docx not installed: {e}") from e
    doc = Document(str(path))
    lines: list[str] = []
    for para in doc.paragraphs:
        style = (para.style.name or "").lower() if para.style else ""
        text = para.text.strip()
        if not text:
            lines.append("")
            continue
        if style.startswith("heading"):
            # "heading 1" -> level 1
            try:
                level = int(style.split()[-1])
            except (ValueError, IndexError):
                level = 2
            lines.append(f"{'#' * max(1, min(level, 6))} {text}")
        else:
            lines.append(text)
    # Tables
    for table in doc.tables:
        lines.append("")
        for row in table.rows:
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
    return ConversionResult("\n".join(lines).strip() + "\n", ".md")


def _convert_pdf(path: Path) -> ConversionResult:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError as e:
        raise ConversionError(f"pypdf not installed: {e}") from e
    reader = PdfReader(str(path))
    parts: list[str] = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            txt = page.extract_text() or ""
        except Exception as e:  # noqa: BLE001
            txt = f"<!-- page {i} extract failed: {e} -->"
        parts.append(f"## Page {i}\n\n{txt.strip()}")
    return ConversionResult("\n\n".join(parts).strip() + "\n", ".md")


def _convert_pptx(path: Path) -> ConversionResult:
    try:
        from pptx import Presentation  # type: ignore
    except ImportError as e:
        raise ConversionError(f"python-pptx not installed: {e}") from e
    prs = Presentation(str(path))
    parts: list[str] = []
    for idx, slide in enumerate(prs.slides, start=1):
        parts.append(f"## Slide {idx}")
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text:
                parts.append(shape.text.strip())
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                parts.append(f"\n> Notes: {notes}")
        parts.append("")
    return ConversionResult("\n".join(parts).strip() + "\n", ".md")


def _convert_xlsx(path: Path) -> ConversionResult:
    try:
        from openpyxl import load_workbook  # type: ignore
    except ImportError as e:
        raise ConversionError(f"openpyxl not installed: {e}") from e
    import csv
    import io

    wb = load_workbook(str(path), data_only=True, read_only=True)
    out = io.StringIO()
    writer = csv.writer(out)
    for sheet in wb.worksheets:
        writer.writerow([f"# sheet: {sheet.title}"])
        for row in sheet.iter_rows(values_only=True):
            writer.writerow(["" if v is None else v for v in row])
        writer.writerow([])
    return ConversionResult(out.getvalue(), ".csv")


def _convert_csv(path: Path) -> ConversionResult:
    return ConversionResult(path.read_text(encoding="utf-8-sig", errors="replace"), ".csv")


_REGISTRY: dict[str, Callable[[Path], ConversionResult]] = {
    ".md": _convert_text,
    ".markdown": _convert_text,
    ".txt": _convert_plain,
    ".rst": _convert_plain,
    ".yaml": _convert_plain,
    ".yml": _convert_plain,
    ".json": _convert_plain,
    ".docx": _convert_docx,
    ".pdf": _convert_pdf,
    ".pptx": _convert_pptx,
    ".xlsx": _convert_xlsx,
    ".csv": _convert_csv,
    ".tsv": _convert_csv,
}


def convert(path: Path) -> ConversionResult:
    """Convert a source file to AI-friendly text (Markdown / CSV)."""
    suffix = path.suffix.lower()
    handler = _REGISTRY.get(suffix)
    if not handler:
        raise ConversionError(f"unsupported format: {suffix}")
    return handler(path)
