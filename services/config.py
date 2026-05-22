"""Configuration loader.

Resolution priority (highest first):
  1. CLI overrides (passed in explicitly)
  2. Environment variable KNOWLEDGE_BASE_ROOT
  3. config.yaml next to the package root
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"


@dataclass
class ChunkingConfig:
    max_chars: int = 1200
    overlap_chars: int = 150
    version: str = "chunking-v1"


@dataclass
class EmbeddingConfig:
    provider: str = "local"
    model: str = "Qwen/Qwen3-Embedding-0.6B"
    dimension: int = 1024
    normalize: bool = True
    query_prompt_name: str | None = "query"


@dataclass
class ConverterConfig:
    version: str = "converter-v1"
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class MCPConfig:
    enable_maintenance_tools: bool = False
    default_search_mode: str = "hybrid"


@dataclass
class Config:
    knowledge_base_root: Path
    documents_dir: Path
    processed_dir: Path
    logs_dir: Path
    documents_data: Path
    database_data: Path
    fulltext_index_dir: Path
    vector_index_dir: Path
    fulltext_engine: str
    vector_engine: str
    chunking: ChunkingConfig
    embedding: EmbeddingConfig
    converter: ConverterConfig
    mcp: MCPConfig
    raw: dict[str, Any]

    def ensure_dirs(self) -> None:
        for d in (
            self.documents_dir,
            self.processed_dir,
            self.logs_dir,
            self.database_data.parent,
            self.fulltext_index_dir,
            self.vector_index_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p)))


def load_config(
    config_path: Path | str | None = None,
    knowledge_base_root_override: Path | str | None = None,
) -> Config:
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f) or {}

    # Resolve KB root: CLI > env > config
    kb_root_raw = (
        str(knowledge_base_root_override)
        if knowledge_base_root_override
        else os.environ.get("KNOWLEDGE_BASE_ROOT")
        or data.get("knowledge_base_root", "%USERPROFILE%\\knowledges")
    )
    kb_root = _expand(kb_root_raw).resolve()

    paths = data.get("paths", {})
    documents_dir = kb_root / paths.get("documents_dir", "documents")
    processed_dir = kb_root / paths.get("processed_dir", "processed")
    logs_dir = kb_root / paths.get("logs_dir", "logs")
    documents_data = kb_root / paths.get("documents_data", "index/documents.csv")
    database_data = kb_root / paths.get("database_data", "index/database.sqlite")
    fulltext_index_dir = kb_root / paths.get("fulltext_index_dir", "index/fulltext")
    vector_index_dir = kb_root / paths.get("vector_index_dir", "index/vector")

    engines = data.get("engines", {})
    chunking_raw = data.get("chunking", {}) or {}
    embedding_raw = data.get("embedding", {}) or {}
    converter_raw = data.get("converter", {}) or {}
    mcp_raw = data.get("mcp", {}) or {}

    return Config(
        knowledge_base_root=kb_root,
        documents_dir=documents_dir,
        processed_dir=processed_dir,
        logs_dir=logs_dir,
        documents_data=documents_data,
        database_data=database_data,
        fulltext_index_dir=fulltext_index_dir,
        vector_index_dir=vector_index_dir,
        fulltext_engine=engines.get("fulltext", "tantivy"),
        vector_engine=engines.get("vector", "lancedb"),
        chunking=ChunkingConfig(**chunking_raw) if chunking_raw else ChunkingConfig(),
        embedding=EmbeddingConfig(**embedding_raw) if embedding_raw else EmbeddingConfig(),
        converter=ConverterConfig(**converter_raw) if converter_raw else ConverterConfig(),
        mcp=MCPConfig(**mcp_raw) if mcp_raw else MCPConfig(),
        raw=data,
    )
