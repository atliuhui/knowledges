"""RapidOCR image-to-Markdown branch of the text pipeline.

Routes ``.png`` / ``.jpg`` / ``.jpeg`` to a local RapidOCR engine (ONNX Runtime,
fully offline) and appends MarkItDown's EXIF/file-metadata block so downstream
search can also match on camera / file metadata.

The module exposes:

* :data:`IMAGE_OCR_SUFFIXES` — file extensions handled by this branch.
* :func:`convert_image` — produce Markdown for a single image.
* :func:`preload` — warm the RapidOCR ONNX model cache once at install time.

See ``services/text_pipeline.py`` for the dispatcher entry point.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._text_base import ConversionError, _markitdown_text


_RAPID_OCR_CACHE: dict[tuple[str, str, str, str], Any] = {}
IMAGE_OCR_SUFFIXES: frozenset[str] = frozenset({".png", ".jpg", ".jpeg"})


def _get_rapid_ocr(version: str, model_type: str, lang_det: str, lang_rec: str) -> Any:
    """Build and cache a RapidOCR engine per (version, model_type, det, rec)."""
    key = (version, model_type, lang_det, lang_rec)
    cached = _RAPID_OCR_CACHE.get(key)
    if cached is not None:
        return cached

    rapid_cls = None
    enums_mod: Any = None
    try:
        from rapidocr import RapidOCR as _RapidOCRNew  # type: ignore
        rapid_cls = _RapidOCRNew
        try:
            import rapidocr as enums_mod  # type: ignore
        except ImportError:
            enums_mod = None
    except ImportError:
        try:
            from rapidocr_onnxruntime import RapidOCR as _RapidOCRLegacy  # type: ignore
            rapid_cls = _RapidOCRLegacy
        except ImportError as e:
            raise ConversionError(
                "rapidocr not installed: reinstall the project with `pip install -e .`"
            ) from e

    def _resolve_enum(enum_name: str, value: str) -> Any:
        if enums_mod is None:
            return value
        enum_cls = getattr(enums_mod, enum_name, None)
        if enum_cls is None:
            return value
        norm = value.strip().upper().replace("-", "").replace("_", "")
        for member in enum_cls:  # type: ignore[attr-defined]
            mname = member.name.upper().replace("-", "").replace("_", "")
            if mname == norm:
                return member
            mval = str(getattr(member, "value", "")).upper().replace("-", "").replace("_", "")
            if mval == norm:
                return member
        return value  # let RapidOCR raise a clearer error if truly invalid

    params = {
        "Det.ocr_version": _resolve_enum("OCRVersion", version),
        "Det.model_type": _resolve_enum("ModelType", model_type),
        "Det.lang_type": _resolve_enum("LangDet", lang_det),
        "Rec.ocr_version": _resolve_enum("OCRVersion", version),
        "Rec.model_type": _resolve_enum("ModelType", model_type),
        "Rec.lang_type": _resolve_enum("LangRec", lang_rec),
    }
    attempts: list[dict[str, Any]] = [{"params": params}, {}]
    last_err: Exception | None = None
    ocr = None
    for kwargs in attempts:
        try:
            ocr = rapid_cls(**kwargs)
            break
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
    if ocr is None:
        raise ConversionError(f"failed to initialize RapidOCR: {last_err}")

    _RAPID_OCR_CACHE[key] = ocr
    return ocr


def _rapid_extract_texts(raw_result: Any) -> list[str]:
    """Normalize RapidOCR output across 1.x / 2.x return shapes into a text list."""
    lines: list[str] = []
    if raw_result is None:
        return lines
    if hasattr(raw_result, "txts"):
        for t in (getattr(raw_result, "txts", None) or []):
            s = str(t).strip()
            if s:
                lines.append(s)
        return lines
    if isinstance(raw_result, tuple):
        raw_result = raw_result[0]
    if not raw_result:
        return lines
    for entry in raw_result:
        try:
            text = entry[1]
        except (IndexError, TypeError):
            continue
        if isinstance(text, (list, tuple)) and text:
            text = text[0]
        s = str(text).strip() if text is not None else ""
        if s:
            lines.append(s)
    return lines


def _resolve_options(options: dict[str, Any]) -> tuple[str, str, str, str]:
    version = str(options.get("image_ocr_version") or "PP-OCRv5").strip() or "PP-OCRv5"
    model_type = str(options.get("image_ocr_model_type") or "mobile").strip() or "mobile"
    lang_det = str(options.get("image_ocr_lang_det") or "ch").strip() or "ch"
    lang_rec = str(options.get("image_ocr_lang_rec") or "ch").strip() or "ch"
    return version, model_type, lang_det, lang_rec


def convert_image(path: Path, options: dict[str, Any]) -> str:
    """RapidOCR text + MarkItDown EXIF/metadata, combined into one Markdown doc."""
    version, model_type, lang_det, lang_rec = _resolve_options(options)
    ocr = _get_rapid_ocr(version, model_type, lang_det, lang_rec)
    try:
        raw = ocr(str(path))
    except Exception as e:  # noqa: BLE001
        raise ConversionError(f"rapidocr failed on {path.name}: {e}") from e
    ocr_lines = _rapid_extract_texts(raw)

    try:
        metadata_md = _markitdown_text(path).strip()
    except ConversionError:
        metadata_md = ""

    parts: list[str] = [f"# {path.stem}", "", "## OCR", ""]
    if ocr_lines:
        parts.extend(ocr_lines)
    else:
        parts.append(f"<!-- no text recognized in {path.name} -->")
    if metadata_md:
        parts.extend(["", "## Metadata", "", metadata_md])
    return "\n".join(parts)


def preload(options: dict[str, Any]) -> dict[str, Any]:
    """Force RapidOCR to download / mmap its ONNX models once."""
    version, model_type, lang_det, lang_rec = _resolve_options(options)
    _get_rapid_ocr(version, model_type, lang_det, lang_rec)
    return {
        "engine": "rapidocr",
        "version": version,
        "model_type": model_type,
        "lang_det": lang_det,
        "lang_rec": lang_rec,
    }
