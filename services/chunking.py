"""Text chunking for indexing."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Chunk:
    chunk_id: str
    text: str
    start: int
    end: int


def chunk_text(text: str, max_chars: int = 1200, overlap_chars: int = 150) -> list[Chunk]:
    """Sliding-window chunker that prefers to break on paragraph or sentence boundaries.

    The chunk_id is `c-NNNN` (zero-padded). Pair it with `doc_id` to get a global id.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be > 0")
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be in [0, max_chars)")
    text = text or ""
    n = len(text)
    if n == 0:
        return []

    chunks: list[Chunk] = []
    step = max_chars - overlap_chars
    start = 0
    idx = 0
    while start < n:
        end = min(start + max_chars, n)
        # Try to break on a nearby boundary
        if end < n:
            window = text[start:end]
            for sep in ("\n\n", "\n", "。", ". ", "! ", "? ", "；", ";"):
                pos = window.rfind(sep)
                if pos != -1 and pos > max_chars * 0.5:
                    end = start + pos + len(sep)
                    break
        chunk_text_str = text[start:end].strip()
        if chunk_text_str:
            chunks.append(
                Chunk(chunk_id=f"c-{idx:04d}", text=chunk_text_str, start=start, end=end)
            )
            idx += 1
        if end >= n:
            break
        start = max(end - overlap_chars, start + step)
    return chunks
