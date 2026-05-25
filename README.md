# 本地 AI 知识库说明

本文档说明本地知识库的数据目录、Python MCP Server 代码目录、数据流、脚本职责和 AI Agent 集成方式。该知识库采用“数据与代码分离”的设计：知识库数据存放在 `%USERPROFILE%\knowledges\`，Python 工具、服务代码和 MCP Server 存放在 `mcp_server\`。

系统采用显式流水线模型：只有执行 `scan.py` 后，系统才会承认原始文档状态发生变化，并触发后续转换和索引流程。

## 目录结构

### 知识库数据目录

```text
%USERPROFILE%\knowledges\
  documents\        # 原始文档，人工维护
    ...
  processed\        # 去格式文档，工具维护
    ...
  index\
    documents.csv   # 源文档快照 + 人工语义元数据
    database.sqlite # 工具维护的运行态元数据，SQLite3
    fulltext\       # 全文索引，默认 Tantivy，未来可升级为 Meilisearch
    vector\         # 向量索引，默认 LanceDB，未来可升级为 Qdrant
  logs\             # 扫描、转换、索引过程记录
```

### Python MCP Server 代码目录

```text
knowledges\
  .venv\            # Python 虚拟环境
  tools\
    scan.py         # 扫描 documents\，更新 index\documents.csv
    convert.py      # 根据 index\documents.csv 更新 processed\ 和 database.sqlite
    ingest.py       # 更新 Tantivy 全文索引和 LanceDB 向量索引
    search.py       # 本地查询调试入口
    metadata.py     # 本地文档元数据维护调试入口
  services\
    config.py
    paths.py
    hashing.py
    database.py
    metadata.py
    metadata_editing.py
    conversion.py
    chunking.py
    fulltext_tantivy.py
    vector_lancedb.py
    hybrid_search.py
  pyproject.toml
  server.py         # MCP Server 入口
  config.yaml       # 配置知识库根目录，默认 %USERPROFILE%\knowledges\
  README.md         # 知识库使用说明
```

## 核心原则

1. `%USERPROFILE%\knowledges\` 是知识库数据实例，只存放文档、转换产物、索引和日志。
2. `mcp_server\` 是代码目录，存放 Python 工具、服务模块、MCP Server 和配置。
3. `documents\` 是原始事实来源，由人工维护，工具不得修改、删除或移动原始文档。
4. `index\documents.csv` 是源文档层的状态边界，包含文件快照字段和人工语义字段。
5. `processed\` 是 AI 友好文本层，由工具从原始文档转换生成，可随时重建。
6. `index\database.sqlite` 是工具维护的运行态元数据，用于记录转换、索引、hash、路径和错误状态。
7. `index\fulltext\` 是关键词检索层，默认使用 Tantivy。
8. `index\vector\` 是语义检索层，默认使用 LanceDB。
9. Agent 不直接操作数据目录，而是通过 MCP 工具访问知识库能力。

## 数据流

```text
%USERPROFILE%\knowledges\documents\ 文件树
  ↓ mcp_server\tools\scan.py
%USERPROFILE%\knowledges\index\documents.csv
  ↓ mcp_server\tools\convert.py
%USERPROFILE%\knowledges\processed\ + index\database.sqlite
  ↓ mcp_server\tools\ingest.py
%USERPROFILE%\knowledges\index\fulltext\ + index\vector\
  ↓ mcp_server\server.py
AI Agent 通过 MCP 查询和引用
```

## config.yaml

`mcp_server\config.yaml` 用于配置知识库数据根目录和索引实现。

示例：

```yaml
knowledge_base_root: "%USERPROFILE%\\knowledges"

paths:
  documents_dir: "documents"
  processed_dir: "processed"
  logs_dir: "logs"
  documents_data: "index\\documents.csv"
  database_data: "index\\database.sqlite"
  fulltext_index_dir: "index\\fulltext"
  vector_index_dir: "index\\vector"

engines:
  fulltext: "tantivy"
  vector: "lancedb"

chunking:
  max_chars: 1200
  overlap_chars: 150
  version: "chunking-v1"

embedding:
  # 通过本地 Ollama 服务做 embedding，运行 `ollama serve` 并预先 `ollama pull qwen3-embedding:0.6b`
  provider: "ollama"
  # Qwen3-Embedding 系列，默认 0.6B；后续可升级到 4B 或 8B
  model: "qwen3-embedding:0.6b"
  dimension: 1024
  normalize: true
  # Ollama 服务端点；留空则使用默认 http://127.0.0.1:11434 或 OLLAMA_HOST 环境变量
  host: null

mcp:
  enable_maintenance_tools: false
  default_search_mode: "hybrid"
```

配置优先级建议：

```text
命令行参数 > 环境变量 KNOWLEDGE_BASE_ROOT > config.yaml
```

## index\documents.csv 定位

`index\documents.csv` 用于描述原始文档的当前快照和人工语义信息。它既不是完整运行态数据库，也不是纯人工清单，而是原始文档进入知识库流水线的确认边界。

推荐字段：

```csv
id,source_path,source_hash,source_size,source_mtime,title,type,tags,confidentiality,status,discovered_at,scanned_at,notes
```

字段说明：

| 字段 | 维护者 | 说明 |
|---|---|---|
| `id` | 工具首次生成，人工可校正 | 文档稳定 ID，后续不应随意变化 |
| `source_path` | 工具 | 原始文档相对路径，必须位于 `documents\` 下 |
| `source_hash` | 工具 | 原始文件内容 hash，用于判断内容是否变化 |
| `source_size` | 工具 | 原始文件大小，用于快速判断和排查 |
| `source_mtime` | 工具 | 原始文件最后修改时间，仅作辅助信息 |
| `title` | 人工 | 文档标题 |
| `type` | 人工 | 文档类型，例如 `note`、`proposal`、`meeting`、`policy` |
| `tags` | 人工 | 标签，建议用分号分隔，例如 `project-a;architecture` |
| `confidentiality` | 人工 | 敏感级别，例如 `public`、`internal`、`confidential`、`private` |
| `status` | 人工或工具 | 文档状态，例如 `active`、`archived`、`ignored`、`missing` |
| `discovered_at` | 工具 | 首次发现时间 |
| `scanned_at` | 工具 | 最近扫描时间 |
| `notes` | 人工 | 备注 |

示例：

```csv
id,source_path,source_hash,source_size,source_mtime,title,type,tags,confidentiality,status,discovered_at,scanned_at,notes
kb-000001,projects\a\proposal.docx,sha256:...,248391,2026-05-21T15:30:00+08:00,项目A方案,proposal,project-a;architecture,internal,active,2026-05-21T16:00:00+08:00,2026-05-21T17:00:00+08:00,
```

## index\database.sqlite 定位

`index\database.sqlite` 是工具维护的运行态数据库，用于记录转换状态、全文索引状态、向量索引状态和错误信息。它不应该由人工直接编辑。

建议记录的信息包括：

| 字段 | 说明 |
|---|---|
| `id` | 对应 `index\documents.csv` 中的文档 ID |
| `source_path` | 原始文档路径 |
| `source_hash` | 最近一次转换时使用的源文档 hash |
| `processed_path` | 转换后的文件路径 |
| `processed_hash` | 转换后文件 hash |
| `converter_version` | 转换脚本或转换器版本 |
| `convert_fingerprint` | `source_hash + converter_version + convert_options` 的组合指纹 |
| `convert_status` | `pending`、`ok`、`failed`、`skipped` |
| `convert_error` | 转换失败原因 |
| `converted_at` | 最近转换时间 |
| `chunking_version` | 文档分块策略版本 |
| `fulltext_engine` | 全文检索实现，例如 `tantivy` |
| `fulltext_index_version` | 全文索引 schema 或索引策略版本 |
| `fulltext_index_status` | `pending`、`ok`、`failed`、`skipped` |
| `fulltext_index_error` | 全文索引失败原因 |
| `fulltext_indexed_at` | 最近全文索引时间 |
| `vector_engine` | 向量检索实现，例如 `lancedb` |
| `embedding_model` | embedding 模型名称或版本 |
| `vector_index_version` | 向量索引 schema、分块或 embedding 策略版本 |
| `vector_index_status` | `pending`、`ok`、`failed`、`skipped` |
| `vector_index_error` | 向量索引失败原因 |
| `vector_indexed_at` | 最近向量索引时间 |

## 检索层选型

### 全文索引：index\fulltext\

`index\fulltext\` 默认使用 Tantivy。它负责关键词检索、短语检索、BM25 排序和片段召回。

中文分词：Tantivy 自带的默认 tokenizer 不会切分 CJK 文本，因此在写入和查询前都会用 jieba（精确模式）对 `title` 和 `text` 字段做预分词，把中文切成词后以空格连接交给 Tantivy。英文、数字、路径等 ASCII 串保持原样。原文另存于 `title_raw` / `text_raw` 字段，仅用于回显 snippet，不参与检索。升级 jieba 词典或修改分词策略时，需要同时提升 `SCHEMA_VERSION`（当前 `fulltext-v2`），并执行 `python -m tools.ingest --force` 重建全文索引。

Tantivy 的定位：

1. 适合本地嵌入式全文检索。
2. 比 SQLite FTS5 更接近专用搜索引擎。
3. 适合和 Agent 工具或本地服务集成。
4. 未来如果需要更强的搜索体验、拼写容错、HTTP API 或产品化检索，可升级为 Meilisearch。

全文检索层应返回统一结果格式：

```text
doc_id
chunk_id
score
snippet
source_path
processed_path
```

### 向量索引：index\vector\

`index\vector\` 默认使用 LanceDB。它负责 embedding 存储、语义相似度检索和元数据过滤。

LanceDB 的定位：

1. 适合本地 RAG 和 AI Agent 场景。
2. 可以把 chunk 文本、embedding 和元数据放在同一张表中。
3. 比 FAISS 更容易管理 metadata。
4. 比 Qdrant 更轻量，适合作为本地默认实现。
5. 未来如果需要多客户端访问、服务化 API 或更强并发能力，可升级为 Qdrant。

向量检索层建议按 chunk 存储：

```text
doc_id
chunk_id
text
embedding
title
tags
source_path
processed_path
confidentiality
processed_hash
```

### Embedding 模型选型

向量索引通过本地 [Ollama](https://ollama.com/) 服务调用 embedding 模型，Python 侧只保留轻量的 `ollama` 客户端，不再依赖 `sentence-transformers` 与 `torch`。默认使用 **Qwen3-Embedding-0.6B**，后续可按需升级到更大规模的同系列模型：

| Ollama 模型标签 | 向量维度 | 适用场景 |
|---|---|---|
| `qwen3-embedding:0.6b`（默认） | 1024 | 本地开发、笔记本、CPU 或入门 GPU |
| `qwen3-embedding:4b` | 2560 | 中等规模知识库，需要更强语义召回 |
| `qwen3-embedding:8b` | 4096 | 大规模知识库，对召回精度要求最高 |

升级方式：修改 `config.yaml` 中的 `embedding.model` 与 `embedding.dimension`，然后执行 `python -m tools.ingest --force` 重建向量索引。

要点：

1. 文档（chunk）入库时按普通文本编码，由 `ingest.py` 调用 `OllamaEmbedder.embed_documents(texts)` 完成。
2. 查询时调用 `OllamaEmbedder.embed_query(text)`，Qwen3-Embedding 的查询指令模板由 Ollama 模型文件内置，无需在客户端再传 `prompt_name`。
3. 默认开启客户端 L2 归一化（`embedding.normalize: true`），与 LanceDB 默认的距离函数兼容。

切换到非 Qwen 系列（例如 Ollama 上的 `bge-m3`、`nomic-embed-text` 等）时，只需把 `model` 改成对应的 Ollama 标签，并按模型实际维度调整 `dimension`。

#### 启动 Ollama 与拉取模型

先安装并启动 [Ollama](https://ollama.com/download)，确保 `ollama serve` 在后台运行（Windows 桌面版默认会以服务方式启动）。

```powershell
# 拉取默认模型
ollama pull qwen3-embedding:0.6b

# 升级到更大的模型
ollama pull qwen3-embedding:4b

# 验证服务可用
ollama list
```

如需把 Ollama 部署到另一台机器或自定义端口，可通过以下方式配置：

| 配置方式 | 说明 |
|---|---|
| `config.yaml` 中的 `embedding.host` | 显式指定，例如 `http://192.168.1.10:11434` |
| 环境变量 `OLLAMA_HOST` | 客户端默认会读取（当 `host: null` 时） |

离线/内网部署：在能访问网络的机器上 `ollama pull <model>` 后，将 Ollama 的模型目录（默认 `%USERPROFILE%\.ollama\models`）拷贝到目标机器同路径即可。

### 图片 OCR 模型选型

`.png` / `.jpg` / `.jpeg` 走 `services\conversion.py` 中的 `_convert_image()`，底层调用 [RapidOCR](https://github.com/RapidAI/RapidOCR)。RapidOCR 使用与 PaddleOCR 同源的 PP-OCR 模型权重，但推理后端为 **onnxruntime**，是纯 Python wheel，全平台覆盖（Windows / Linux / macOS Intel & Apple Silicon），不需要 PaddlePaddle。

默认使用 **PP-OCRv5 mobile** 组合（CPU 友好、ONNX 模型总计约 21 MB，覆盖简体/繁体中文 + 英文 + 日文）：

| 阶段 | 默认模型 | 大小 | 控制字段（`converter.options.*`） |
|---|---|---|---|
| 文本检测 | `PP-OCRv5_mobile_det` | ~4.7 MB | `image_ocr_version` + `image_ocr_model_type` + `image_ocr_lang_det` |
| 方向分类 | `ch_ppocr_mobile_v2.0_cls` | ~0.6 MB | RapidOCR 默认启用 |
| 文本识别 | `PP-OCRv5_mobile_rec` | ~16 MB | `image_ocr_version` + `image_ocr_model_type` + `image_ocr_lang_rec` |

可选取值：

- `image_ocr_version`：`PP-OCRv5`、`PP-OCRv4`、`PP-OCRv3`。
- `image_ocr_model_type`：`mobile`（CPU 推荐）、`server`（需更多内存/GPU）。
- `image_ocr_lang_det`：检测阶段语言，通常 `ch`（适用中英、多语种）或 `en`。
- `image_ocr_lang_rec`：识别阶段语言。v5 默认覆盖中英日繁；v3 下还可选 `japan`、`korean`、`chinese_cht`、`latin`、`arabic`、`cyrillic`、`devanagari` 等。

更换任何字段会自动改变 `convert_fingerprint`，下一次 `kb-convert` 会重算受影响的图片。

#### 预加载模型

RapidOCR 随主依赖一起装好（`pip install -e .`），无需额外 extras。

首次运行 `kb-convert` 时 RapidOCR 会按需下载 ONNX 模型到包内默认路径（`<site-packages>/rapidocr/models/`）或用户缓存目录。为了避免在第一张图上出现可见卡顿，可在安装后执行一次预加载，把检测/识别模型一次性拉到本地：

```powershell
python -c "from rapidocr import OCRVersion, ModelType, LangDet, LangRec, RapidOCR; RapidOCR(params={'Det.ocr_version':OCRVersion.PPOCRV5,'Det.model_type':ModelType.MOBILE,'Det.lang_type':LangDet.CH,'Rec.ocr_version':OCRVersion.PPOCRV5,'Rec.model_type':ModelType.MOBILE,'Rec.lang_type':LangRec.CH})"
```

该命令仅做模型下载与一次性初始化，不读取任何图片；之后 `kb-convert` 在 CPU 上的单张推理约 100–300 ms。

离线/内网部署：在能访问网络的机器上跑一次上述预加载命令，再将所生成的 RapidOCR 模型目录（位于虚拟环境下 `Lib\site-packages\rapidocr\models\`）拷贝到目标机器同路径即可。

## Python 工具职责

### mcp_server\tools\scan.py

职责：

1. 读取 `documents\` 文件树。
2. 计算每个源文件的 `source_hash`、`source_size`、`source_mtime`。
3. 新文件追加到 `index\documents.csv`。
4. 已存在文件更新快照字段。
5. 缺失文件标记为 `missing`，而不是直接删除记录。
6. 保留人工维护字段，例如 `title`、`type`、`tags`、`confidentiality`、`notes`。
7. 校验 `id` 唯一性、路径合法性、重复文件、状态值和密级值。

不应做的事：

1. 不转换文档。
2. 不写入 `processed\`。
3. 不写入 `index\`。
4. 不修改原始文档内容。

### mcp_server\tools\convert.py

职责：

格式对应关系（输入 -> 输出）：

| 输入格式 | 输出格式 | 处理说明 |
|---|---|---|
| `.csv` | `.csv` | 规范化读取，UTF-8-SIG 兼容 |
| `.docx` | `.md` | 提取正文与表格并转换为 Markdown |
| `.yml` / `.yaml` | `.yml` / `.yaml` | 文本规范化，统一 UTF-8 编码 |
| `.drawio` | `.md` | 提取图中可读文本并转换为 Markdown |
| `.pdf` | `.md` | 按页提取文本并转换为 Markdown |
| `.xmind` | `.md` | 提取主题结构并转换为 Markdown |
| `.txt` | `.txt` | 文本规范化，统一 UTF-8 编码 |
| `.md` / `.markdown` | `.md` / `.markdown` | 文本规范化，统一 UTF-8 编码 |
| `.pptx` | `.md` | 提取幻灯片与备注并转换为 Markdown |
| `.json` | `.json` | 文本规范化，统一 UTF-8 编码 |
| `.xlsx` | `.csv` | 按工作表导出为 CSV |
| `.png` / `.jpg` / `.jpeg` | `.md` | 通过 RapidOCR（PP-OCRv5 mobile / ONNX）提取图中文本并写入 Markdown |

1. 读取 `index\documents.csv`。
2. 跳过 `status = ignored` 或 `status = archived` 的文档。
3. 查询 `index\database.sqlite` 中的历史转换状态。
4. 根据 `source_hash`、`converter_version`、`convert_options` 生成 `convert_fingerprint`。
5. 当 `convert_fingerprint` 变化或 `processed\` 文件缺失时，重新转换文档。
6. 将 Word、PDF、PPT、Draw.io、XMind 等格式转换为 Markdown，将 Excel 转换为 CSV；TXT、YAML、JSON、Markdown 等文本类文件在保持原后缀的前提下做规范化并统一为 UTF-8 编码输出。
7. 更新 `processed\` 文件。
8. 更新 `index\database.sqlite` 中的转换状态、hash、路径和错误信息。

不应做的事：

1. 不修改 `documents\` 下的原始文档。
2. 不更新全文索引。
3. 不更新向量索引。

### mcp_server\tools\ingest.py

职责：

1. 读取 `index\database.sqlite`。
2. 找出 `convert_status = ok` 且需要重新索引的文档。
3. 读取 `processed\` 中的 AI 友好文本。
4. 对文档进行 chunk 切分。
5. 更新 `index\fulltext\` 中的 Tantivy 全文索引。
6. 生成或更新 embedding。
7. 更新 `index\vector\` 中的 LanceDB 向量索引。
8. 写回 `fulltext_index_status`、`vector_index_status`、错误信息和索引时间。

不应做的事：

1. 不读取或修改原始文档。
2. 不修改 `index\documents.csv`。
3. 不执行文档格式转换。

### mcp_server\tools\search.py

职责：

1. 用于本地命令行调试检索效果。
2. 调用 `services\hybrid_search.py`。
3. 组合 Tantivy 全文检索和 LanceDB 语义检索结果。
4. 返回 JSON 格式的候选 chunk、score、snippet 和来源路径。

### mcp_server\tools\metadata.py

职责：

1. 用于本地命令行调试 metadata 维护能力。
2. 调用 `services\metadata_editing.py`。
3. 支持列出文档、列出标签、预览 metadata 修改、应用 metadata 修改。
4. 只允许修改人工维护字段，不允许修改工具维护字段。

## MCP 工具设计

MCP Server 通过 `mcp_server\server.py` 暴露知识库能力给 AI Agent。

### 只读工具

默认开放：

```text
kb.search
kb.get_document
kb.get_chunk
kb.get_metadata
kb.list_documents
kb.list_tags
kb.suggest_metadata
kb.preview_metadata_update
kb.apply_metadata_update
kb.bulk_preview_metadata_update
kb.bulk_apply_metadata_update
kb.warmup
```

其中查询类工具只读取索引、processed 文本和元数据；metadata 更新类工具只修改 `index\documents.csv` 中的人工维护字段，并且必须遵循“先 preview、再 apply”的确认流程。`kb.warmup` 用于在执行 `kb.search` 等开销较大的工具前预热 Tantivy、LanceDB、jieba 和 Ollama embedding 模型，详见“冷启动与预热”一节。

### 维护工具

可选开放：

```text
kb.scan
kb.convert
kb.ingest
kb.rebuild
kb.run_pipeline
```

`kb.run_pipeline` 会按顺序执行 `scan -> convert -> ingest`，并支持参数：

```text
force      # 对 convert/ingest 传递 --force
only       # 仅处理指定 doc id 列表
no_vector  # ingest 跳过向量索引
```

这些工具会修改 `index\documents.csv`、`processed\` 或 `index\`，建议通过 `config.yaml` 中的 `mcp.enable_maintenance_tools` 控制，默认关闭。

### 冷启动与预热

MCP server 自身启动很快，但**第一次** `kb.search` / `kb.get_chunk` 会触发以下一次性开销：

1. Ollama 第一次收到 embedding 请求时把模型权重装入 CPU/GPU（0.6B ≈ 1–3 秒，4B/8B 更久）；
2. `lancedb` 与 `pyarrow` 首次 import；
3. `jieba` 词典首次加载；
4. Tantivy / LanceDB 索引首次打开。

为避免 Agent 在用户面前出现可见的卡顿，可在一次会话开始时（或在批量调用 `kb.search` 之前）先调用 `kb.warmup` 工具：

```json
{
  "name": "kb.warmup",
  "arguments": {}
}
```

返回示例：

```json
{
  "ok": true,
  "total_ms": 2143.5,
  "stages": [
    {"stage": "jieba",    "ok": true, "elapsed_ms": 320.1},
    {"stage": "fulltext", "ok": true, "elapsed_ms": 95.6},
    {"stage": "vector",   "ok": true, "elapsed_ms": 180.3, "dim": 1024},
    {"stage": "embedder", "ok": true, "elapsed_ms": 1547.5, "model": "qwen3-embedding:0.6b", "vector_len": 1024, "keep_alive": null}
  ]
}
```

`kb.warmup` 是只读、幂等的；重复调用几乎零成本（embedder 实例和索引句柄都会在模块层缓存）。

要让 Ollama 加载后的模型不被自动卸载，可在 `config.yaml` 中设置：

```yaml
embedding:
  keep_alive: "24h"   # 或 -1 表示常驻
```

该值会随每次 embed 请求传给 Ollama，覆盖其默认的 5 分钟空闲卸载策略。

## Agent 驱动的 metadata 维护

本项目不单独提供 HTML5 管理界面。metadata 维护采用 Agent-driven UI 模式：

```text
用户
  ↓ 在 Agent 界面中查看表格、建议和确认项
AI Agent
  ↓ 调用 MCP preview 工具
KnowledgeBase MCP Server
  ↓ 返回待修改内容
AI Agent
  ↓ 用户确认后调用 MCP apply 工具
index\documents.csv
```

MCP 只提供结构化数据能力，Agent 负责在对话界面中生成可操作的表格、候选项、批量确认和修改预览。所有实际写入统一由 MCP 工具完成。

### 可编辑字段

metadata 维护工具只允许修改人工维护字段：

```text
title
type
tags
confidentiality
status
notes
```

禁止修改工具维护字段：

```text
id
source_path
source_hash
source_size
source_mtime
discovered_at
scanned_at
```

### 推荐交互流程

示例：整理没有标签的文档。

```text
用户：帮我整理没有 tag 的文档。
  ↓
Agent 调用 kb.list_documents，筛选 missing_tags = true。
  ↓
Agent 调用 kb.suggest_metadata，生成建议标签和类型。
  ↓
Agent 展示候选表格和建议修改。
  ↓
用户确认接受、编辑或跳过。
  ↓
Agent 调用 kb.preview_metadata_update 或 kb.bulk_preview_metadata_update。
  ↓
Agent 展示 before/after 修改预览。
  ↓
用户最终确认。
  ↓
Agent 调用 kb.apply_metadata_update 或 kb.bulk_apply_metadata_update。
```

### kb.list_documents

用于列出文档并按 metadata 条件筛选。

输入示例：

```json
{
  "filter": {
    "status": ["active"],
    "missing_tags": true,
    "missing_title": false,
    "tags_include": ["project-a"],
    "tags_exclude": ["archived"],
    "confidentiality": ["public", "internal"]
  },
  "limit": 50,
  "offset": 0
}
```

输出示例：

```json
{
  "documents": [
    {
      "id": "kb-001",
      "title": "项目A方案",
      "source_path": "documents\\project-a\\proposal.docx",
      "type": "proposal",
      "tags": ["project-a"],
      "confidentiality": "internal",
      "status": "active",
      "notes": ""
    }
  ]
}
```

### kb.list_tags

返回已有标签和使用次数。

输出示例：

```json
{
  "tags": [
    {
      "tag": "project-a",
      "count": 12
    },
    {
      "tag": "architecture",
      "count": 5
    }
  ]
}
```

### kb.suggest_metadata

根据文件路径、标题、已有标签、processed 内容摘要和现有标签集，建议标题、类型、标签、密级或状态。

输出示例：

```json
{
  "doc_id": "kb-001",
  "suggested": {
    "type": "proposal",
    "tags": ["project-a", "architecture", "proposal"],
    "confidentiality": "internal"
  },
  "confidence": 0.84,
  "reason": "文件路径和正文中多次出现 Project A 与 architecture。"
}
```

### kb.preview_metadata_update

只返回即将发生的变化，不写入文件。Agent 必须先展示该结果，再请求用户确认。

输入示例：

```json
{
  "id": "kb-001",
  "patch": {
    "title": "项目A架构方案",
    "type": "proposal",
    "tags": ["project-a", "architecture"],
    "confidentiality": "internal",
    "status": "active",
    "notes": "已人工确认"
  }
}
```

输出示例：

```json
{
  "changes": [
    {
      "id": "kb-001",
      "source_path": "documents\\project-a\\proposal.docx",
      "before": {
        "tags": []
      },
      "after": {
        "tags": ["project-a", "architecture"]
      }
    }
  ],
  "requires_ingest": true,
  "reason": "tags/confidentiality changed and indexes need metadata refresh"
}
```

### kb.apply_metadata_update

在用户确认后执行单文档 metadata 修改。

输入示例：

```json
{
  "id": "kb-001",
  "patch": {
    "tags": ["project-a", "architecture"],
    "status": "active"
  }
}
```

输出示例：

```json
{
  "updated": 1,
  "requires_ingest": true
}
```

### kb.bulk_preview_metadata_update 与 kb.bulk_apply_metadata_update

用于批量修改 metadata。批量 apply 前必须先 preview。

输入示例：

```json
{
  "ids": ["kb-001", "kb-002"],
  "operation": {
    "add_tags": ["project-a"],
    "remove_tags": ["untagged"],
    "set_confidentiality": "internal",
    "set_status": "active"
  }
}
```

批量操作必须展示：

1. 将修改多少个文档。
2. 每个文档修改哪些字段。
3. 是否影响 `confidentiality` 或 `status`。
4. 是否需要重新执行 `ingest.py`。

### 修改后的索引刷新

如果只修改以下人工语义字段，通常不需要重新执行 `convert.py`：

```text
title
type
tags
confidentiality
status
notes
```

但通常需要重新执行 `ingest.py` 或局部刷新索引元数据，以确保 Tantivy 和 LanceDB 中的 metadata 与 `index\documents.csv` 保持一致。

## 增量更新规则

### 原始文档发生变化

```text
用户修改 documents\ 中的文件
  ↓
执行 mcp_server\tools\scan.py
  ↓
index\documents.csv 中的 source_hash 变化
  ↓
执行 mcp_server\tools\convert.py
  ↓
processed\ 和 index\database.sqlite 更新
  ↓
执行 mcp_server\tools\ingest.py
  ↓
Tantivy 全文索引和 LanceDB 向量索引更新
```

### 原始文档未变化

如果 `source_hash` 未变化，且 `convert_fingerprint` 未变化，`convert.py` 应跳过转换。

### 转换规则变化

如果转换器版本、转换参数或转换脚本逻辑变化，应更新 `converter_version` 或 `convert_options`，从而生成新的 `convert_fingerprint`。即使 `source_hash` 不变，也可以触发重新转换。

### 分块规则变化

如果 chunk 大小、重叠策略、标题拼接策略或表格处理规则变化，应更新 `chunking_version`。`ingest.py` 应重新生成 chunk，并同步更新全文索引和向量索引。

### 全文索引规则变化

如果 Tantivy schema、字段权重、tokenizer、BM25 参数或索引格式变化，应更新 `fulltext_index_version`。`ingest.py` 应重建受影响的全文索引。

### 向量索引规则变化

如果 embedding 模型、向量维度、归一化方式、距离函数或 LanceDB schema 变化，应更新 `vector_index_version` 或 `embedding_model`。`ingest.py` 应重新生成 embedding 并更新向量索引。

## processed\ 路径规则

`processed\` 应尽量镜像 `documents\` 的目录结构，方便追溯原文。

示例：

```text
documents\projects\a\proposal.docx
processed\projects\a\proposal.md

documents\meetings\2026-05-21-review.pptx
processed\meetings\2026-05-21-review.md

documents\data\customers.xlsx
processed\data\customers.csv
```

## AI Agent 使用规则

AI Agent 使用知识库时应遵守以下规则：

1. 通过 MCP 工具访问知识库，不直接操作 `%USERPROFILE%\knowledges\`。
2. 优先组合查询 `index\fulltext\` 和 `index\vector\`。
3. 对精确词、短语、编号、术语、文件名，优先使用全文检索。
4. 对概念、主题、相似内容、自然语言问题，优先使用向量检索。
5. 需要读取正文时，优先读取 `processed\` 中的 Markdown、TXT、CSV。
6. 需要确认原始格式或附件时，再引用 `documents\` 中的源文件。
7. 回答基于知识库的问题时，必须给出来源路径。
8. 不得修改、删除或移动 `documents\` 中的原始文档。
9. 不得将 `confidentiality = confidential` 或 `private` 的内容发送到外部系统。
10. 检索结果不足时，应说明未找到充分依据，不得编造。

建议给 Agent 的提示词：

```text
你可以通过 KnowledgeBase MCP Server 使用本地知识库。
优先调用 kb.search 组合使用全文索引和向量索引。
精确关键词、短语、编号和文件名使用全文检索。
概念性、主题性和相似内容查询使用向量检索。
读取内容时优先使用 processed\ 中的 Markdown、TXT、CSV。
如需确认原文，再引用 documents\ 中的原始文件。
回答时必须列出来源路径。
不要修改 documents\ 中的任何原始文件。
如果没有找到依据，请明确说明未找到相关资料。
```

## 推荐执行顺序

在 `mcp_server\` 目录中执行。

首次初始化：

```powershell
ollama pull qwen3-embedding:0.6b
# 可选：如果 documents\ 中包含 .png/.jpg/.jpeg，可先预加载 RapidOCR 模型避免首张卡顿
python -c "from rapidocr import OCRVersion, ModelType, LangDet, LangRec, RapidOCR; RapidOCR(params={'Det.ocr_version':OCRVersion.PPOCRV5,'Det.model_type':ModelType.MOBILE,'Det.lang_type':LangDet.CH,'Rec.ocr_version':OCRVersion.PPOCRV5,'Rec.model_type':ModelType.MOBILE,'Rec.lang_type':LangRec.CH})"

python .\tools\scan.py
python .\tools\convert.py
python .\tools\ingest.py
```

也可以使用全流程脚本（仓库内 `actions\run-pipeline.ps1`）：

```powershell
.\actions\run-pipeline.ps1

# 强制重跑 convert/ingest
.\actions\run-pipeline.ps1 -Force

# 只处理指定文档 id
.\actions\run-pipeline.ps1 -Only kb-000001,kb-000002

# 跳过向量索引
.\actions\run-pipeline.ps1 -NoVector
```
日常更新：

```powershell
python .\tools\scan.py
python .\tools\convert.py
python .\tools\ingest.py
```

本地查询调试：

```powershell
python .\tools\search.py "项目A 架构方案"
```

启动 MCP Server：

```powershell
python .\server.py
```

如果只修改了标签、标题、密级等人工语义字段，通常不需要重新转换原始文档，但需要重新执行 `ingest.py`，以便全文索引和向量索引中的元数据保持一致。

## 日志

所有工具应将运行日志写入知识库数据目录：

```text
%USERPROFILE%\knowledges\logs\scan.log
%USERPROFILE%\knowledges\logs\convert.log
%USERPROFILE%\knowledges\logs\ingest.log
%USERPROFILE%\knowledges\logs\server.log
```

日志至少应包含：

1. 运行开始和结束时间。
2. 处理文件数量。
3. 新增、更新、跳过、失败数量。
4. 失败文件路径和错误原因。
5. 使用的脚本版本、转换器版本、全文索引版本、embedding 模型版本。

## 可重建性

该知识库应满足以下可重建原则：

1. `documents\` 和 `index\documents.csv` 是最重要的持久状态。
2. `processed\` 可以由 `convert.py` 重建。
3. `index\database.sqlite` 可以由 `index\documents.csv` 和 `processed\` 重建。
4. `index\fulltext\` 可以由 `ingest.py` 重建。
5. `index\vector\` 可以由 `ingest.py` 重建。
6. 如果索引损坏，应优先重建索引，而不是修改原始文档。

## 维护建议

1. 原始文档统一放入 `%USERPROFILE%\knowledges\documents\`，不要直接放入 `processed\`。
2. 修改原始文档后，先执行 `scan.py`。
3. 人工标签、分类、密级建议维护在 `index\documents.csv` 中。
4. 批量导入新文档后，先运行 `scan.py`，确认 `index\documents.csv` 中新增记录正确，再运行 `convert.py` 和 `ingest.py`。
5. 定期备份 `documents\` 和 `index\documents.csv`。
6. `processed\`、`index\fulltext\` 和 `index\vector\` 可以备份，但应视为可重建产物。
7. `mcp_server\` 应单独进行版本管理，避免与知识库数据备份混在一起。

## 代码初始化

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

```sh
pip install -e .

ollama pull qwen3-embedding:0.6b

python -c "from rapidocr import OCRVersion, ModelType, LangDet, LangRec, RapidOCR; RapidOCR(params={'Det.ocr_version':OCRVersion.PPOCRV5,'Det.model_type':ModelType.MOBILE,'Det.lang_type':LangDet.CH,'Rec.ocr_version':OCRVersion.PPOCRV5,'Rec.model_type':ModelType.MOBILE,'Rec.lang_type':LangRec.CH})"
```

```sh
python -m tools.scan
python -m tools.convert
python -m tools.ingest

python -m tools.search "MCF document"
```

```powershell
.\actions\run-pipeline.ps1
```

```powershell
npx @modelcontextprotocol/inspector .\.venv\Scripts\python.exe -m server
```

> **重要**：本项目所有 Python 代码（`tools\*.py`、`server.py`、`hf` 等）都必须在当前项目的 `.venv` 虚拟环境中运行，不要使用全局 Python。每次新开终端先执行 `.\.venv\Scripts\Activate.ps1` 激活环境，确认提示符前出现 `(.venv)` 后再运行任何命令；AI Agent 协助执行命令时也应遵循同样规则，避免污染全局环境或引入版本冲突。
