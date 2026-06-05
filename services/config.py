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
    # `provider` is informational; the only supported backend is a local
    # Ollama server. `host` may be None to use the ollama client's default
    # (http://127.0.0.1:11434 or the OLLAMA_HOST env var).
    provider: str = "ollama"
    model: str = "qwen3-embedding:0.6b"
    dimension: int = 1024
    normalize: bool = True
    host: str | None = None
    # Forwarded to Ollama's `keep_alive`; e.g. "30m", "24h", or -1 to keep the
    # model resident indefinitely. None uses Ollama's default (5 minutes).
    keep_alive: str | int | None = None


@dataclass
class TextPipelineConfig:
    version: str = "text-v1"
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class DataPipelineConfig:
    """Settings for the Parquet/DuckDB lane.

    ``version`` is baked into ``convert_fingerprint`` so changing data lane
    rules (column inference, sheet split policy, ...) triggers a re-run.
    """
    version: str = "data-v1"
    sample_rows: int = 5


@dataclass
class IngestConfig:
    # Documents are ingested sequentially. Within a single document, this many
    # parallel embedding requests are fanned out to the Ollama server (chunks
    # are split into N shards). Tune this together with OLLAMA_NUM_PARALLEL.
    concurrency: int = 4


@dataclass
class MCPConfig:
    enable_maintenance_tools: bool = False
    default_search_mode: str = "hybrid"


@dataclass
class AppsConfig:
    """Settings for the loopback HTTP server hosting H5 offline apps.

    The actual file root is ``<knowledge_base_root>/<paths.apps_dir>``;
    this dataclass only carries the network-facing fields used by the
    local web server scripts (start-pocketbase / start-miniserve) and the kb_create_app / kb_list_apps tools.
    """
    host: str = "127.0.0.1"
    port: int = 8788
    # Explicit base URL override (e.g. "http://127.0.0.1:8788" behind a
    # reverse proxy). When None, callers should compose http://{host}:{port}.
    base_url: str | None = None

    def resolved_base_url(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        return f"http://{self.host}:{self.port}"


_DEFAULT_DATA_SUFFIXES: tuple[str, ...] = (
    ".csv", ".tsv", ".xlsx", ".xls",
    ".json", ".xml", ".yaml", ".yml",
)


@dataclass
class ScanConfig:
    """Settings for `tools.scan` and downstream data-class routing."""
    data_suffixes: list[str] = field(default_factory=lambda: list(_DEFAULT_DATA_SUFFIXES))



@dataclass
class Config:
    knowledge_base_root: Path
    docs_dir: Path
    text_dir: Path
    data_dir: Path
    logs_dir: Path
    apps_dir: Path
    apps_data_dir: Path
    docs_data: Path
    db_data: Path
    fulltext_index_dir: Path
    vector_index_dir: Path
    fulltext_engine: str
    vector_engine: str
    chunking: ChunkingConfig
    embedding: EmbeddingConfig
    text_pipeline: TextPipelineConfig
    data_pipeline: DataPipelineConfig
    ingest: IngestConfig
    mcp: MCPConfig
    scan: ScanConfig
    apps: AppsConfig
    raw: dict[str, Any]

    def ensure_dirs(self) -> None:
        for d in (
            self.docs_dir,
            self.text_dir,
            self.data_dir,
            self.logs_dir,
            self.apps_dir,
            self.db_data.parent,
            self.fulltext_index_dir,
            self.vector_index_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p)))


def _build_text_pipeline(raw: dict[str, Any]) -> "TextPipelineConfig":
    """Build TextPipelineConfig from a flat YAML mapping.

    The YAML layout is flat (no nested ``options:``); everything except
    ``version`` is collected into the ``options`` dict that downstream
    converters consume.
    """
    if not raw:
        return TextPipelineConfig()
    raw = dict(raw)
    version = raw.pop("version", None) or "text-v1"
    return TextPipelineConfig(version=version, options=raw)


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
    docs_dir = kb_root / paths.get("docs_dir", "docs")
    text_dir = kb_root / paths.get("text_dir", "text")
    data_dir = kb_root / paths.get("data_dir", "data")
    logs_dir = kb_root / paths.get("logs_dir", "logs")
    apps_dir = kb_root / paths.get("apps_dir", "apps")
    apps_data_dir = kb_root / paths.get("apps_data_dir", "apps_data")
    docs_data = kb_root / paths.get("docs_data", "store/docs.csv")
    db_data = kb_root / paths.get("db_data", "store/db.sqlite")
    fulltext_index_dir = kb_root / paths.get("fulltext_index_dir", "store/fulltext")
    vector_index_dir = kb_root / paths.get("vector_index_dir", "store/vector")

    engines = data.get("engines", {})
    chunking_raw = data.get("chunking", {}) or {}
    embedding_raw = data.get("embedding", {}) or {}
    text_pipeline_raw = data.get("text_pipeline", {}) or {}
    data_pipeline_raw = data.get("data_pipeline", {}) or {}
    ingest_raw = data.get("ingest", {}) or {}
    mcp_raw = data.get("mcp", {}) or {}
    scan_raw = data.get("scan", {}) or {}
    apps_raw = data.get("apps", {}) or {}

    return Config(
        knowledge_base_root=kb_root,
        docs_dir=docs_dir,
        text_dir=text_dir,
        data_dir=data_dir,
        logs_dir=logs_dir,
        apps_dir=apps_dir,
        apps_data_dir=apps_data_dir,
        docs_data=docs_data,
        db_data=db_data,
        fulltext_index_dir=fulltext_index_dir,
        vector_index_dir=vector_index_dir,
        fulltext_engine=engines.get("fulltext", "tantivy"),
        vector_engine=engines.get("vector", "lancedb"),
        chunking=ChunkingConfig(**chunking_raw) if chunking_raw else ChunkingConfig(),
        embedding=EmbeddingConfig(**embedding_raw) if embedding_raw else EmbeddingConfig(),
        text_pipeline=_build_text_pipeline(text_pipeline_raw),
        data_pipeline=DataPipelineConfig(**data_pipeline_raw) if data_pipeline_raw else DataPipelineConfig(),
        ingest=IngestConfig(**ingest_raw) if ingest_raw else IngestConfig(),
        mcp=MCPConfig(**mcp_raw) if mcp_raw else MCPConfig(),
        scan=ScanConfig(**scan_raw) if scan_raw else ScanConfig(),
        apps=AppsConfig(**apps_raw) if apps_raw else AppsConfig(),
        raw=data,
    )
