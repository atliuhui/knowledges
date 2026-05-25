"""Embedding client backed by a local Ollama server.

The knowledge base used to bundle `sentence-transformers` + `torch` for local
inference. We now delegate embedding to an external Ollama process so the
Python footprint stays small and GPU/CPU management is handled by Ollama.

Usage:
    embedder = OllamaEmbedder(cfg.embedding)
    vecs = embedder.embed_documents(["hello", "world"])
    qvec = embedder.embed_query("hello")
"""

from __future__ import annotations

import math
from typing import Sequence

from .config import EmbeddingConfig


class OllamaEmbedder:
    """Thin wrapper around the official `ollama` Python client.

    Notes:
    - Qwen3-Embedding's instruction template is handled inside the Ollama
      model file, so callers do not need to pass a `prompt_name`.
    - When `normalize=True`, vectors are L2-normalized client-side so that
      LanceDB's cosine/L2 distance behaves consistently regardless of what
      the server returns.
    """

    def __init__(self, cfg: EmbeddingConfig) -> None:
        try:
            import ollama  # type: ignore
        except ImportError as e:  # pragma: no cover - import guard
            raise RuntimeError(
                "ollama python package not installed; run `pip install ollama`."
            ) from e

        self._model = cfg.model
        self._normalize = cfg.normalize
        self._client = (
            ollama.Client(host=cfg.host) if cfg.host else ollama.Client()
        )

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = self._client.embed(model=self._model, input=list(texts))
        vecs = list(resp["embeddings"])
        if self._normalize:
            vecs = [_l2_normalize(v) for v in vecs]
        return [[float(x) for x in v] for v in vecs]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def _l2_normalize(vec: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(float(x) * float(x) for x in vec))
    if norm == 0.0:
        return [float(x) for x in vec]
    return [float(x) / norm for x in vec]
