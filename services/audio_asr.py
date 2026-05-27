"""Audio / video transcription branch of the text pipeline.

ffmpeg (system binary on PATH, falling back to imageio-ffmpeg's bundled binary)
normalizes any media file to 16 kHz mono float32 PCM, then sherpa-onnx +
SenseVoice-Small transcribes it offline. MarkItDown is best-effort for an
optional ``## Metadata`` block (EXIF/ID3/container info) — silently skipped
when the format is unsupported.

The module exposes:

* :data:`AUDIO_SUFFIXES`, :data:`VIDEO_SUFFIXES`, :data:`MEDIA_SUFFIXES`.
* :func:`convert_media` — produce Markdown for a single audio / video file.
* :func:`preload` — resolve ffmpeg + build the SenseVoice recognizer once.

See ``services/text_pipeline.py`` for the dispatcher entry point.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from ._text_base import REPO_ROOT, ConversionError, _markitdown_text
from .paths import AUDIO_SUFFIXES, MEDIA_SUFFIXES, VIDEO_SUFFIXES  # re-exported

_SENSE_VOICE_CACHE: dict[tuple[str, str, bool, int], Any] = {}


def _resolve_ffmpeg(explicit: str | None) -> str:
    """Locate an ffmpeg binary: explicit path -> PATH -> imageio-ffmpeg bundle."""
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return str(p)
        raise ConversionError(f"configured ffmpeg_path not found: {explicit}")
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        from imageio_ffmpeg import get_ffmpeg_exe  # type: ignore
    except ImportError as e:
        raise ConversionError(
            "ffmpeg not found on PATH and imageio-ffmpeg is not installed; "
            "reinstall the project with `pip install -e .`"
        ) from e
    try:
        return get_ffmpeg_exe()
    except Exception as e:  # noqa: BLE001
        raise ConversionError(f"imageio-ffmpeg could not provide an ffmpeg binary: {e}") from e


def _ffmpeg_to_pcm_f32(path: Path, ffmpeg_bin: str) -> Any:
    """Decode any audio/video file to 16 kHz mono float32 PCM samples (numpy)."""
    import numpy as np

    cmd = [
        ffmpeg_bin,
        "-nostdin",
        "-loglevel", "error",
        "-i", str(path),
        "-vn",                # drop video streams
        "-f", "f32le",        # 32-bit little-endian float PCM
        "-ac", "1",           # mono
        "-ar", "16000",       # 16 kHz
        "-",                  # write to stdout
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, check=False,
        )
    except FileNotFoundError as e:
        raise ConversionError(f"ffmpeg binary not executable: {ffmpeg_bin}: {e}") from e
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()[-1:] or [""]
        raise ConversionError(f"ffmpeg failed to decode {path.name}: {err[0]}")
    samples = np.frombuffer(proc.stdout, dtype=np.float32)
    if samples.size == 0:
        raise ConversionError(f"ffmpeg produced 0 audio samples from {path.name}")
    return samples


def _resolve_audio_model_dir(raw: str) -> Path:
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = (REPO_ROOT / candidate).resolve()
    return candidate


def _get_sense_voice(model_dir: Path, language: str, use_itn: bool, num_threads: int) -> Any:
    """Build and cache a sherpa-onnx OfflineRecognizer for SenseVoice-Small."""
    key = (str(model_dir), language, use_itn, num_threads)
    cached = _SENSE_VOICE_CACHE.get(key)
    if cached is not None:
        return cached

    try:
        import sherpa_onnx  # type: ignore
    except ImportError as e:
        raise ConversionError(
            "sherpa-onnx not installed: reinstall the project with `pip install -e .`"
        ) from e

    if not model_dir.is_dir():
        raise ConversionError(
            f"SenseVoice model directory not found: {model_dir}. "
            "Download a sherpa-onnx SenseVoice-Small bundle (e.g. "
            "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17) and extract it there."
        )
    # Prefer the int8-quantized model on CPU; fall back to full precision.
    model_file = None
    for candidate in ("model.int8.onnx", "model.onnx"):
        if (model_dir / candidate).is_file():
            model_file = model_dir / candidate
            break
    tokens_file = model_dir / "tokens.txt"
    if model_file is None or not tokens_file.is_file():
        raise ConversionError(
            f"SenseVoice model files missing in {model_dir}: "
            "expected `model.int8.onnx` (or `model.onnx`) and `tokens.txt`."
        )

    try:
        recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(  # type: ignore[attr-defined]
            model=str(model_file),
            tokens=str(tokens_file),
            num_threads=num_threads,
            use_itn=use_itn,
            language=language,
        )
    except Exception as e:  # noqa: BLE001
        raise ConversionError(f"failed to initialize sherpa-onnx SenseVoice: {e}") from e

    _SENSE_VOICE_CACHE[key] = recognizer
    return recognizer


def _resolve_options(options: dict[str, Any]) -> tuple[Path, str, bool, int, str]:
    raw_dir = str(options.get("audio_model_dir") or "models/sense-voice")
    model_dir = _resolve_audio_model_dir(raw_dir)
    language = str(options.get("audio_language") or "auto").strip() or "auto"
    use_itn_val = options.get("audio_use_itn", True)
    use_itn = bool(use_itn_val) if not isinstance(use_itn_val, str) else use_itn_val.lower() == "true"
    try:
        num_threads = int(options.get("audio_num_threads") or 4)
    except (TypeError, ValueError):
        num_threads = 4
    if num_threads < 1:
        num_threads = 1
    ffmpeg_bin = _resolve_ffmpeg(str(options.get("ffmpeg_path") or "") or None)
    return model_dir, language, use_itn, num_threads, ffmpeg_bin


def convert_media(path: Path, options: dict[str, Any]) -> str:
    """ffmpeg -> 16 kHz mono PCM -> SenseVoice transcript + optional metadata."""
    model_dir, language, use_itn, num_threads, ffmpeg_bin = _resolve_options(options)

    samples = _ffmpeg_to_pcm_f32(path, ffmpeg_bin)
    recognizer = _get_sense_voice(model_dir, language, use_itn, num_threads)
    try:
        stream = recognizer.create_stream()
        stream.accept_waveform(16000, samples)
        recognizer.decode_stream(stream)
        text = (stream.result.text or "").strip()
    except Exception as e:  # noqa: BLE001
        raise ConversionError(f"sherpa-onnx failed on {path.name}: {e}") from e

    # Best-effort EXIF / ID3 / container metadata via MarkItDown. Video and
    # several exotic audio containers are not supported by MarkItDown; we
    # silently skip the Metadata section when that happens.
    metadata_md = ""
    try:
        metadata_md = _markitdown_text(path).strip()
    except ConversionError:
        metadata_md = ""

    parts: list[str] = [f"# {path.stem}", "", "## Transcript", ""]
    if text:
        parts.append(text)
    else:
        parts.append(f"<!-- no speech recognized in {path.name} -->")
    if metadata_md:
        parts.extend(["", "## Metadata", "", metadata_md])
    return "\n".join(parts)


def preload(options: dict[str, Any]) -> dict[str, Any]:
    """Resolve ffmpeg + build a sherpa-onnx OfflineRecognizer once."""
    model_dir, language, use_itn, num_threads, ffmpeg_bin = _resolve_options(options)
    _get_sense_voice(model_dir, language, use_itn, num_threads)
    return {
        "engine": "sherpa-onnx + sense-voice",
        "model_dir": str(model_dir),
        "language": language,
        "use_itn": use_itn,
        "num_threads": num_threads,
        "ffmpeg": ffmpeg_bin,
    }
