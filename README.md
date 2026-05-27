# 本地 AI 知识库说明

本文档说明本地知识库的数据目录、`mcp_server` 代码目录、数据流、脚本职责和 AI Agent 集成方式。该知识库采用“数据与代码分离”的设计：知识库数据存放在 `%USERPROFILE%\knowledges\`，Python 工具、服务代码和 `mcp_server` 入口存放在当前仓库根目录（示例名 `knowledges\`）。

系统采用显式流水线模型：只有执行 `scan.py` 后，系统才会承认原始文档状态发生变化，并触发后续转换和索引流程。

## 目录结构

### 知识库数据目录

```text
%USERPROFILE%\knowledges\
  docs\        # 原始文档，人工维护
    ...
  text\        # 去格式文档（Markdown），工具维护
    ...
  data\             # 数据类文档的规范化产物（Parquet），工具维护，DuckDB 查询
    ...
  apps\             # H5 离线应用目录，由 Agent 通过 kb.create_app 写入；miniserve 静态托管
    ...
  store\
    docs.csv   # 源文档快照 + 人工语义元数据
    db.sqlite # 工具维护的运行态元数据 + data_tables 注册表，SQLite3
    fulltext\       # 全文索引，默认 Tantivy，未来可升级为 Meilisearch
    vector\         # 向量索引，默认 LanceDB，未来可升级为 Qdrant
  logs\             # 扫描、转换、索引过程记录
```

### `mcp_server` 代码目录（仓库根目录）

```text
knowledges\
  .venv\            # Python 虚拟环境
  vendor\           # 仓库内置二进制（miniserve.exe 等），由 actions\download-*.ps1 拉取
  tools\
    scan.py         # 扫描 docs\，更新 store\docs.csv
    convert.py      # 根据 store\docs.csv 更新 text\ 和 db.sqlite
    ingest.py       # 更新 Tantivy 全文索引和 LanceDB 向量索引
    search.py       # 本地查询调试入口
    data.py         # 本地 Parquet/DuckDB 数据表调试入口
    metadata.py     # 本地文档元数据维护调试入口
    app.py          # 本地 H5 离线应用调试入口（list/create/url）
  services\
    config.py
    paths.py
    locking.py
    hashing.py
    database.py
    metadata.py
    metadata_editing.py
    _text_base.py     # 文本流水线共享基类（ConversionResult/Error、MarkItDown 包装）
    text_pipeline.py  # 文本流水线调度器
    image_ocr.py      # RapidOCR 图片分支
    audio_asr.py      # sherpa-onnx + SenseVoice + ffmpeg 音视频分支
    metadata_preview.py  # data-class 文件的 metadata-only Markdown 预览
    data_pipeline.py
    data_query.py
    chunking.py
    embeddings.py
    fulltext_tantivy.py
    vector_lancedb.py
    hybrid_search.py
    apps.py           # H5 离线应用目录维护：slug 校验、安全写入、列出
  pyproject.toml
  server.py         # `mcp_server` 入口
  config.yaml       # 配置知识库根目录，默认 %USERPROFILE%\knowledges\
  README.md         # 知识库使用说明
```

## 核心原则

1. `%USERPROFILE%\knowledges\` 是知识库数据实例，只存放文档、转换产物、索引和日志。
2. 当前仓库根目录（示例 `knowledges\`）是代码目录，存放 Python 工具、服务模块、`mcp_server` 入口和配置。
3. `docs\` 是原始事实来源，由人工维护，工具不得修改、删除或移动原始文档。
4. `store\docs.csv` 是源文档层的状态边界，包含文件快照字段和人工语义字段。
5. `text\` 是 AI 友好文本层，由工具从原始文档转换生成，可随时重建。
6. `store\db.sqlite` 是工具维护的运行态元数据，用于记录转换、索引、hash、路径和错误状态。
7. `store\fulltext\` 是关键词检索层，默认使用 Tantivy。
8. `store\vector\` 是语义检索层，默认使用 LanceDB。
9. Agent 不直接操作数据目录，而是通过 MCP 工具访问知识库能力。
10. `data\` 是数据类文档（CSV / Excel / JSON / XML / YAML）的规范化层：标记为 `data` 标签的文档会被 `tools.convert` 转写为 Parquet 并登记到 `db.sqlite` 的 `data_tables` 表，由 MCP `kb.list_data_tables` / `kb.read_data_table` / `kb.query_data`（DuckDB 只读）暴露。

## 数据流

```text
%USERPROFILE%\knowledges\docs\ 文件树
  ↓ tools\scan.py
%USERPROFILE%\knowledges\store\docs.csv
  ↓ tools\convert.py
%USERPROFILE%\knowledges\text\ + store\db.sqlite
  ↓ tools\ingest.py
%USERPROFILE%\knowledges\store\fulltext\ + store\vector\
  ↓ server.py
AI Agent 通过 MCP 查询和引用
```

## config.yaml

`config.yaml` 用于配置知识库数据根目录和索引实现。

示例：

```yaml
knowledge_base_root: "%USERPROFILE%\\knowledges"

paths:
  docs_dir: "docs"
  text_dir: "text"
  data_dir: "data"          # Parquet 规范化产物落盘目录
  logs_dir: "logs"
  docs_data: "store\\docs.csv"
  db_data: "store\\db.sqlite"
  fulltext_index_dir: "store\\fulltext"
  vector_index_dir: "store\\vector"

data_pipeline:
  version: "data-v1"        # 数据流水线版本，参与 convert fingerprint
  sample_rows: 5            # metadata-only md 的样本行数

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

## store\docs.csv 定位

`store\docs.csv` 用于描述原始文档的当前快照和人工语义信息。它既不是完整运行态数据库，也不是纯人工清单，而是原始文档进入知识库流水线的确认边界。

推荐字段：

```csv
id,source_path,source_hash,source_size,source_mtime,title,type,tags,confidentiality,status,discovered_at,scanned_at,notes
```

字段说明：

| 字段 | 维护者 | 说明 |
|---|---|---|
| `id` | 工具首次生成，人工可校正 | 文档稳定 ID，后续不应随意变化 |
| `source_path` | 工具 | 原始文档相对路径，必须位于 `docs\` 下 |
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

## store\db.sqlite 定位

`store\db.sqlite` 是工具维护的运行态数据库，用于记录转换状态、全文索引状态、向量索引状态和错误信息。它不应该由人工直接编辑。

建议记录的信息包括：

| 字段 | 说明 |
|---|---|
| `id` | 对应 `store\docs.csv` 中的文档 ID |
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

### 全文索引：store\fulltext\

`store\fulltext\` 默认使用 Tantivy。它负责关键词检索、短语检索、BM25 排序和片段召回。

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

### 向量索引：store\vector\

`store\vector\` 默认使用 LanceDB。它负责 embedding 存储、语义相似度检索和元数据过滤。

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

#### 调整 Ollama 并行度（加速 `ingest`）

`tools/ingest.py` 已改造为多文档并发处理（`config.yaml` 中 `ingest.concurrency`，默认 `4`；也可用 `kb-ingest --concurrency N` 临时覆盖）。但要真正吃到 embedding 并行，必须把 **Ollama 服务端**的并行度同步调大——默认值往往是 `OLLAMA_NUM_PARALLEL=1`，会把多线程请求串行化。

先确认当前并行度：

```powershell
# Windows 默认日志位置（注意不是 logs\ 子目录）
Get-Content "$env:LOCALAPPDATA\Ollama\server.log" `
  | Select-String "OLLAMA_NUM_PARALLEL|OLLAMA_MAX_LOADED" `
  | Select-Object -Last 3
```

输出里若看到 `OLLAMA_NUM_PARALLEL:1`，按下面步骤调高（推荐 4，与本仓库 ingest 默认 concurrency 对齐）：

```powershell
# 1) 写入用户级环境变量（持久生效）
setx OLLAMA_NUM_PARALLEL 4
setx OLLAMA_MAX_LOADED_MODELS 2   # 可选：允许同时常驻多个模型（如 embed + LLM）

# 2) 完全关闭 Ollama（托盘 Quit；或强杀进程）
Get-Process ollama* -ErrorAction SilentlyContinue | Stop-Process -Force

# 3) 重新启动 Ollama（点桌面/托盘图标，或命令行 `ollama serve`）
#    setx 设置的变量只对此后启动的新进程生效

# 4) 验证已生效
Get-Content "$env:LOCALAPPDATA\Ollama\server.log" -Tail 80 `
  | Select-String "cls|OLLAMA_MAX_LOADED"
# 期望看到： OLLAMA_NUM_PARALLEL:4   OLLAMA_MAX_LOADED_MODELS:2
```

并行度对资源占用的影响：每个并行槽位会按 `Parallel × KvSize` 单独分配 KV cache（embedding 模型很小，4 路并行基本无压力；若改用 4B/8B 的 LLM，需根据显存酌情下调）。`OLLAMA_MAX_LOADED_MODELS` 决定同时常驻的模型数，配合 `embedding.keep_alive: -1` 可让 embed 模型常驻、避免冷启动。

### 图片 OCR 模型选型

`.png` / `.jpg` / `.jpeg` 在 `services\text_pipeline.py`（具体分支实现位于 `services\image_ocr.py`）中走 [RapidOCR](https://github.com/RapidAI/RapidOCR) 提取图中文字，然后用 MarkItDown 追加 EXIF / 文件元数据，合并为一份 Markdown（顶部是 `# {文件名}` 一级标题，下方依次 `## OCR` 与 `## Metadata` 两节）。RapidOCR 使用与 PaddleOCR 同源的 PP-OCR 模型权重，但推理后端为 **onnxruntime**，是纯 Python wheel，全平台覆盖（Windows / Linux / macOS Intel & Apple Silicon），不需要 PaddlePaddle。

默认使用 **PP-OCRv5 mobile** 组合（CPU 友好、ONNX 模型总计约 21 MB，覆盖简体/繁体中文 + 英文 + 日文）：

| 阶段 | 默认模型 | 大小 | 控制字段（`text_pipeline.*`） |
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

首次运行 `kb-convert` 时 RapidOCR 会按需下载 ONNX 模型到包内默认路径（`<site-packages>/rapidocr/models/`，即仓库自带的 `.venv` 内部）。为了避免在第一张图上出现可见卡顿，可在安装后执行一次预加载（该命令会读取 `config.yaml -> text_pipeline` 中的 `image_ocr_*` 选项，保证与实际转换路径一致）：

```powershell
python -m services.text_pipeline image
```

该命令仅做模型下载与一次性初始化，不读取任何图片；之后 `kb-convert` 在 CPU 上的单张推理约 100–300 ms。

离线/内网部署：在能访问网络的机器上跑一次上述预加载命令，再将所生成的 RapidOCR 模型目录（位于虚拟环境下 `Lib\site-packages\rapidocr\models\`）拷贝到目标机器同路径即可。

### 音频/视频转写引擎选型

音频 (`.wav` / `.mp3` / `.m4a` / `.aac` / `.flac` / `.ogg` / `.opus` / `.wma` / `.amr`) 和视频 (`.mp4` / `.mkv` / `.mov` / `.avi` / `.webm` / `.flv` / `.m4v` / `.wmv` / `.ts` / `.mpg` / `.mpeg`) 在 `services\text_pipeline.py`（具体分支实现位于 `services\audio_asr.py`）中走统一路径：

1. **ffmpeg** 把任意容器/采样率/声道数解码为 **16 kHz 单声道 float32 PCM**（视频自动丢弃画面流）；
2. **[sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)** 加载 **[SenseVoice-Small](https://github.com/FunAudioLLM/SenseVoiceSmall)** 模型做端到端语音识别，生成 Markdown（顶部是 `# {文件名}` 一级标题，下方依次 `## Transcript` 与 `## Metadata` 两节，元数据继续用 MarkItDown 抽取 ID3/容器信息，best-effort）。

选 SenseVoice-Small 的理由是它在中文场景下**精度优于同体量 Whisper、推理速度快一个数量级**，且原生支持中/英/日/韩/粤五语种 + 逆文本归一化 (ITN) + 标点。下表横向对比三类主流开源离线 ASR：

| 引擎 / 模型 | 架构 | 参数量 | 语言覆盖 | 中文 CER (AISHELL-1) | CPU RTF (4 线程, int8) | 标点 / ITN | 流式 | 模型大小 (int8) | 备注 |
|---|---|---|---|---|---|---|---|---|---|
| **SenseVoice-Small** (本仓库默认) | 非自回归 Encoder + CTC | ~234 M | zh / en / ja / ko / yue | **~3.0%** | **~0.07** | 内置 | 否（一次性整段） | ~240 MB | 中文优势明显；情感/事件标签可选 |
| Paraformer-large (FunASR / sherpa-onnx) | 非自回归 Encoder + Predictor | ~220 M | zh + en | ~3.3% (zh) | ~0.10 | 需外接 punc 模型 | 有流式版 | ~220 MB | 中文老牌强基线；多语种需另起模型 |
| Whisper-large-v3 (faster-whisper / whisper.cpp) | 自回归 Encoder-Decoder | ~1550 M | 99 种 | ~5–8% | ~0.5–1.0 | 内置 | 否（自回归） | ~1.5 GB (int8) | 多语种最广；中文短句易漏；CPU 慢 |
| Whisper-small | 自回归 Encoder-Decoder | ~244 M | 99 种 | ~10–15% | ~0.20 | 内置 | 否 | ~244 MB | 体量接近 SenseVoice，但中文 CER 高一档 |
| Whisper-tiny | 自回归 Encoder-Decoder | ~39 M | 99 种 | ~20%+ | ~0.05 | 内置 | 否 | ~39 MB | 极快但中文质量不可用于检索 |

> RTF (Real-Time Factor) 越低越快；0.1 表示 10 倍速。CER 与 RTF 数据综合自各项目官方 Benchmark（AISHELL-1 / SpeechIO），实际值随硬件而变化，仅作量级参考。

如果你需要：

- **更多语种**（如阿拉伯语、印地语）—— 改用 Whisper-large-v3（自行下载 ONNX/GGML 权重，替换 `services/audio_asr.py` 中的引擎实现）。
- **流式 / 麦克风实时识别** —— 改用 Paraformer 的流式版本（`sherpa-onnx-streaming-paraformer-*`）。
- **更低资源** —— 在配置里把 `audio_num_threads` 调小，或换用 Whisper-tiny（牺牲精度）。

本仓库当前只接 SenseVoice-Small；切换其他引擎需要改代码，不只是改 `config.yaml`。

### 音频/视频转写配置

相关字段全部位于 `config.yaml -> text_pipeline`：

| 字段 | 默认值 | 说明 |
|---|---|---|
| `audio_model_dir` | `models/sense-voice` | sherpa-onnx SenseVoice 模型目录。相对路径基于仓库根目录解析；绝对路径直接使用 |
| `audio_language` | `auto` | `auto` / `zh` / `en` / `ja` / `ko` / `yue` |
| `audio_use_itn` | `true` | 是否做逆文本归一化（数字、日期等自动转阿拉伯数字 + 标点） |
| `audio_num_threads` | `4` | onnxruntime CPU 线程数；显著影响推理速度 |
| `ffmpeg_path` | `""` (空) | 显式指定 ffmpeg 二进制；为空时优先用系统 PATH，再回退到 [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) 自带二进制 |

这些字段全都参与 `convert_fingerprint`，修改后下次 `kb-convert` 会重算受影响的音视频文档。

### 下载 SenseVoice-Small 模型

sherpa-onnx 官方在 GitHub Release 上提供打包好的 ONNX 模型（中/英/日/韩/粤）。仓库提供 `actions\download-sense-voice.ps1` 完成下载 + 解压 + 重命名，默认落到 `<repo>\models\sense-voice\`（与 mcp_server runtime 同处一地）：

```powershell
# 默认包：sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17
.\actions\download-sense-voice.ps1

# 重新下载覆盖已有目录
.\actions\download-sense-voice.ps1 -Force

# 自定义发布包 URL 或目标 models 根目录
.\actions\download-sense-voice.ps1 -Url "https://.../<other>.tar.bz2" -ModelsDir D:\shared\models
```

脚本结束后目录里应该有 `model.int8.onnx`（~240 MB）和 `tokens.txt`（~50 KB）。`services\audio_asr.py` 会优先加载 `model.int8.onnx`，若不存在则回退到 `model.onnx`（全精度，~900 MB，CPU 推理慢约 2-3×）。如果你已经在别处下载过模型，可以直接把 `audio_model_dir` 改成那个绝对路径，无需再复制一份。

### 预加载语音识别模型与 ffmpeg

首次 `kb-convert` 一份音频/视频时，sherpa-onnx 会加载 ~240 MB ONNX 权重并初始化 CPU 算子缓存（一次约 1–3 秒）。为避免在首条音频上的可见卡顿，安装后可调用仓库提供的预加载入口，一次性拉起 sherpa-onnx OfflineRecognizer 并定位 ffmpeg 二进制（这是一个仅预热、不读取任何音频的命令）：

```powershell
# 预加载语音识别模型（读取 config.yaml -> text_pipeline 的配置）
python -m services.text_pipeline audio

# 同时预热图片 OCR 与语音识别
python -m services.text_pipeline all
```

该入口会读取当前 `config.yaml` 中的 `audio_model_dir` / `audio_language` / `audio_use_itn` / `audio_num_threads` / `ffmpeg_path`，所以改了 YAML 后不需要同步改预热命令。输出是 JSON，列出实际加载的模型路径、线程数与 ffmpeg 路径，便于排查：

```json
{
  "audio": {
    "engine": "sherpa-onnx + sense-voice",
    "model_dir": "<repo>\\models\\sense-voice",
    "language": "auto",
    "use_itn": true,
    "num_threads": 4,
    "ffmpeg": "<repo>\\.venv\\Lib\\site-packages\\imageio_ffmpeg\\binaries\\ffmpeg-win-x86_64-v7.1.exe"
  }
}
```

上述示例中 `model_dir` 与 `ffmpeg` 都位于仓库内部（`models\` 子目录与 `.venv` 子目录），与 mcp_server runtime 同处一地，方便整体打包、迁移与离线分发；如果系统 PATH 里另装了 ffmpeg，会优先使用 PATH 中的版本。

若模型目录缺失或 ffmpeg 未装，该命令会以非零退出码返回，并在 JSON 里给出 `error` 字段，适合放进 CI / 部署脚本检查。需要手工控制的场景下，也可以直接调用底层函数：

```powershell
# 等价低阶用法：在 REPL/脚本里按需预热
python -c "from services.text_pipeline import preload_audio; print(preload_audio())"
```

离线/内网部署：把 `models\sense-voice\` 整个目录连同 `.venv` 一起拷贝过去；imageio-ffmpeg 的 wheel 在 Windows / Linux / macOS 上都自带二进制，无需联网下载。

## Python 工具职责

### tools\scan.py

职责：

1. 读取 `docs\` 文件树。
2. 计算每个源文件的 `source_hash`、`source_size`、`source_mtime`。
3. 新文件追加到 `store\docs.csv`。
4. 已存在文件更新快照字段。
5. 缺失文件标记为 `missing`，而不是直接删除记录。
6. 保留人工维护字段，例如 `title`、`type`、`tags`、`confidentiality`、`notes`。
7. 校验 `id` 唯一性、路径合法性、重复文件、状态值和密级值。

支持的源文件扩展名集合（`services.paths.SUPPORTED_DOC_SUFFIXES`）由代码固定，覆盖纯文本/标记、Office、图谱/思维导图、邮件/Notebook/压缩包/电子书、图片，以及 `services.text_pipeline` 注册的全部音频/视频容器。该集合不通过 `config.yaml` 暴露——每种扩展名都有专属的转换分支，加入未知扩展名只会让扫描接收文件、转换阶段无法处理。若需新增格式，应在 `services/text_pipeline.py` 中添加分支后再扩展该集合。

不应做的事：

1. 不转换文档。
2. 不写入 `text\`。
3. 不写入 `store\`。
4. 不修改原始文档内容。

### tools\convert.py

职责：

转换底座统一为 [Microsoft MarkItDown](https://github.com/microsoft/markitdown)，所有支持的格式都会被转换成 Markdown（`.md`）写入 `text\`。本仓库**仅使用 MarkItDown 的离线能力**：

1. 严格通过 `MarkItDown.convert_local()` 入口处理本地文件，不会访问任何远程 URI。
2. 构造 MarkItDown 时不传 `llm_client` / `docintel_endpoint` / `cu_endpoint`，禁用图像 LLM 描述、Azure Document Intelligence、Azure Content Understanding 等联网能力。
3. 通过 `enable_plugins=False` 关闭第三方插件，避免插件引入网络调用。
4. **不**安装会联网的可选依赖：`audio-transcription`（默认通过 Google Web Speech API 联网）、`youtube-transcription`、`az-doc-intel`、`az-content-understanding`。

格式对应关系（输入 -> 输出，全部在本机完成，无网络请求）。除注明外，所有转换均由 [MarkItDown](https://github.com/microsoft/markitdown) `convert_local()` 调度到对应 Converter 完成，输出统一为 UTF-8 Markdown：

| 输入格式 | 输出 | MarkItDown 处理逻辑 |
|---|---|---|
| `.pdf` | `.md` | `PdfConverter`：用 `pdfminer.six` 抽取按页正文文本，按段拼接，丢弃图片/字体/布局信息（不做 OCR；扫描版 PDF 会得到空文本） |
| `.docx` | `.md` | `DocxConverter`：用 `mammoth` 把 Word 转 HTML，再交给 `HtmlConverter` 转 Markdown；保留标题层级、列表、表格、超链接；图片以占位符出现 |
| `.pptx` | `.md` | `PptxConverter`：用 `python-pptx` 逐页抽取每个 shape 的文本、表格、备注（speaker notes），按 `### Slide N` 分节输出 |
| `.xlsx` | `.md` | `XlsxConverter`：用 `openpyxl` 读每个 sheet，把单元格区域转成 Markdown 表格，按 `## {sheet 名}` 分节；不计算公式结果以外的样式 |
| `.xls` | `.md` | `XlsConverter`：用 `xlrd` 读旧版 Excel，输出同 `.xlsx` |
| `.csv` / `.tsv` | `.md` | `CsvConverter`：以 UTF-8 解析（见“文本编码”），首行作为表头，整表渲染为单个 Markdown 表格 |
| `.json` | `.md` | `PlainTextConverter` 分支：UTF-8 读入后整体放入 ```` ```json ```` 围栏代码块，不解析结构 |
| `.xml` | `.md` | `RssConverter` 优先匹配 RSS/Atom feed（提取条目标题/链接/摘要为列表）；非 feed 走 `PlainTextConverter`，整体放入 ```` ```xml ```` 围栏 |
| `.html` / `.htm` | `.md` | `HtmlConverter`：用 `BeautifulSoup` 清洗后交 `markdownify` 转 Markdown，保留标题、列表、表格、链接；脚本/样式/外链资源被剥离，不会发起网络请求 |
| `.epub` | `.md` | `EpubConverter`：解包 EPUB，按 spine 顺序逐章把内部 XHTML 喂给 `HtmlConverter`，章节间以 `---` 分隔 |
| `.zip` | `.md` | `ZipConverter`：递归遍历压缩包内文件，对每个成员按其扩展名再走一次 MarkItDown，结果按 `## {成员路径}` 拼接 |
| `.msg` | `.md` | `OutlookMsgConverter`：用 `olefile` 解析 Outlook 邮件，输出收发件人、主题、日期、正文（HTML 正文再走 `HtmlConverter`）和附件清单 |
| `.ipynb` | `.md` | `IpynbConverter`：解析 Jupyter Notebook JSON，按单元格顺序输出 Markdown cell 原文 + ```` ```python ```` 代码 cell；忽略 cell outputs |
| `.md` / `.markdown` / `.txt` / `.rst` / `.yaml` / `.yml` | `.md` | `PlainTextConverter`：UTF-8 读入后原样作为正文返回；不解析语法，仅做编码归一与换行规整 |
| `.png` / `.jpg` / `.jpeg` | `.md` | **不走 MarkItDown 文本提取**：本地 [RapidOCR](https://github.com/RapidAI/RapidOCR) 识别图中文字 + MarkItDown `ImageConverter` 抽取 EXIF/文件元数据后追加（详见“图片 OCR 模型选型”） |
| `.gif` | `.md` | `ImageConverter`：仅提取 EXIF / 文件元数据（不抽帧识别） |
| `.drawio` | `.md` | **不走 MarkItDown**：用标准库 `xml.etree` 解析 Draw.io XML，逐个 `<diagram>` 抽取 `mxCell` 的 `value` 文本作为项目符号；自动处理 deflate + base64 + url-encode 的压缩格式 |
| `.xmind` | `.md` | **不走 MarkItDown**：把 `.xmind` 作为 ZIP 解包，优先读取 `content.json`（新版），按 `rootTopic` 递归遍历主题/子主题/备注生成多级标题；缺失时回退到 `content.xml`（旧版）按 `<topic>` 抽题目 |
| `.wav` / `.mp3` / `.m4a` / `.aac` / `.flac` / `.ogg` / `.opus` / `.wma` / `.amr` | `.md` | **不走 MarkItDown 转写**：本地 [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) + [SenseVoice-Small](https://github.com/FunAudioLLM/SenseVoiceSmall) 转写 + MarkItDown `AudioConverter` 抽取 ID3/容器元数据后追加；非 PCM 容器先经 ffmpeg 解码（详见“音频/视频转写”） |
| `.mp4` / `.mkv` / `.mov` / `.avi` / `.webm` / `.flv` / `.m4v` / `.wmv` / `.ts` / `.mpg` / `.mpeg` | `.md` | ffmpeg 抽取音轨 → SenseVoice-Small 转写（不走 MarkItDown，不做画面/字幕 OCR） |

**文本编码约定**：所有“文本类”格式（`.md / .markdown / .txt / .rst / .yaml / .yml / .json / .xml / .html / .htm / .csv / .tsv`）在 `services/_text_base.py` 中调用 `convert_local()` 时会显式传入 `StreamInfo(charset="utf-8")`，跳过 MarkItDown 自带的 `chardet` 检测。这一步必须保留——MarkItDown 默认在没有 BOM、文件较短或前缀全 ASCII 时，charset 检测会返回 `None` 并回退到 Python 的 `locale.getpreferredencoding()`（Windows 中文区域为 `cp936`、容器/CI 中常为 `ascii`），从而在 UTF-8 中文 `.md` 上抛 `UnicodeDecodeError: 'ascii' codec can't decode byte ...`。如果未来要支持 GBK/GB18030 等其他编码的源文件，应在 `_TEXTISH_MARKITDOWN_SUFFIXES` 旁加上配置化的字符集覆盖，而不是去掉这条 charset 强制约定。

**离线模式下不支持的能力**（需要联网，已主动禁用）：

1. 图像 LLM 内容描述（依赖外部多模态 LLM；OCR 已在本机用 RapidOCR 覆盖）。
2. YouTube / 在线视频链接转写（依赖外部下载与字幕服务）。
3. Azure Document Intelligence 增强 PDF 解析。
4. Azure Content Understanding 多模态分析。
5. 任意远程 URL / HTTP(S) 流的转换（`convert_url` / `convert_stream` 未启用）。

> 如未来需要其中某项能力，应在评估网络与合规要求后，单独引入对应 extras 并在 `services/text_pipeline.py` 中显式开启，而不是默认开启。

1. 读取 `store\docs.csv`。
2. 跳过 `status = ignored` 或 `status = archived` 的文档。
3. 查询 `store\db.sqlite` 中的历史转换状态。
4. 根据 `source_hash`、`converter_version`、`convert_options`、`tags` 决定的 `md_mode` 与 `parquet` 标志、`data_pipeline.version` 生成 `convert_fingerprint`。
5. 当 `convert_fingerprint` 变化或 `text\` 文件缺失时，重新转换文档。
6. 按文件类型 + `tags` 分发到三条离线管线，写入 `text\` 中对应的 `.md` 文件：
   - 图像 (`.png` / `.jpg` / `.jpeg`)：RapidOCR 提取文字 + MarkItDown EXIF；
   - 音频/视频 (`.wav` / `.mp3` / `.mp4` / ...)：ffmpeg 解码为 16 kHz PCM → sherpa-onnx + SenseVoice-Small 转写 + MarkItDown ID3/容器元数据；
   - 其他所有格式：MarkItDown `convert_local()`。
7. 当文档为「数据类」（`.csv / .tsv / .xlsx / .xls / .json / .xml / .yaml / .yml`）且 `tags` 中包含 `data` 时，额外将原文规范化为 Parquet 写入 `data\`，并在 `db.sqlite` 的 `data_tables` 表登记 `table_name / sheet / parquet_path / columns / row_count`。
8. 更新 `text\` 文件。
9. 更新 `store\db.sqlite` 中的转换状态、hash、路径和错误信息。

**标签驱动的 md 输出模式**（在第 6 步执行前评估，结果烘焙进 `convert_fingerprint`）：

| 文件类别 | `tags` 含 `text` | `tags` 含 `data` | `tags` 为空 |
|---|---|---|---|
| 文本类（其它所有支持格式） | 全文 md | 仅元数据 md | 全文 md |
| 数据类（`.csv / .tsv / .xlsx / .xls / .json / .xml / .yaml / .yml`） | 全文 md（整表/原文渲染） | 仅元数据 md + Parquet 落盘到 `data\` | 仅元数据 md |

「仅元数据 md」格式：YAML frontmatter（含 `title / source_path / tags / size / mtime / suffix` 等）+ 一级标题 + 列名清单 + 前 `data_pipeline.sample_rows` 行预览。Parquet 文件统一以 ZSTD 压缩、所有单元格强制 string，便于 DuckDB 跨表查询。

「数据类」扩展名集合由 `config.yaml -> scan.data_suffixes` 控制（默认即上表所列 8 种）。修改该字段会改变文档的 md_mode / parquet 判定，并通过 `convert_fingerprint` 自动触发受影响文档的重新转换。

不应做的事：

1. 不修改 `docs\` 下的原始文档。
2. 不更新全文索引。
3. 不更新向量索引。

### tools\ingest.py

职责：

1. 读取 `store\db.sqlite`。
2. 找出 `convert_status = ok` 且需要重新索引的文档。
3. 读取 `text\` 中的 AI 友好文本。
4. 对文档进行 chunk 切分。
5. 更新 `store\fulltext\` 中的 Tantivy 全文索引。
6. 生成或更新 embedding。
7. 更新 `store\vector\` 中的 LanceDB 向量索引。
8. 写回 `fulltext_index_status`、`vector_index_status`、错误信息和索引时间。

不应做的事：

1. 不读取或修改原始文档。
2. 不修改 `store\docs.csv`。
3. 不执行文档格式转换。

### tools\search.py

职责：

1. 用于本地命令行调试检索效果。
2. 调用 `services\hybrid_search.py`。
3. 组合 Tantivy 全文检索和 LanceDB 语义检索结果。
4. 返回 JSON 格式的候选 chunk、score、snippet 和来源路径。

### tools\metadata.py

职责：

1. 用于本地命令行调试 metadata 维护能力。
2. 调用 `services\metadata_editing.py`。
3. 支持列出文档、列出标签、预览 metadata 修改、应用 metadata 修改。
4. 只允许修改人工维护字段，不允许修改工具维护字段。

### tools\data.py

职责：

1. 用于本地命令行调试 data lane（Parquet + DuckDB）。
2. 调用 `services\database.py` 读取 `data_tables` 注册表，调用 `services\data_query.py` 执行只读 SQL。
3. 提供三个子命令，与 MCP 工具 `kb.list_data_tables` / `kb.read_data_table` / `kb.query_data` 一一对应：
   - `python -m tools.data list [--doc-id ID]`
   - `python -m tools.data read TABLE_NAME [--limit N] [--offset M]`
   - `python -m tools.data query "SELECT ..." [--limit N]`
4. 仅供调试，不修改任何文件或注册表。

### tools\app.py

职责：

1. 用于本地命令行调试 H5 离线应用目录（`apps\`）。
2. 调用 `services\apps.py` 复用与 MCP 相同的 slug 校验和安全写入逻辑。
3. 提供三个子命令：
  - `python -m tools.app list`
  - `python -m tools.app create SLUG [--overwrite] [--files-json PATH] [--title T] [--no-default]`
  - `python -m tools.app url SLUG`
4. `create` 默认会生成最小脚手架（`index.html` + `style.css` + `app.js`）；可用 `--files-json` 追加/覆盖文件。

## MCP 工具设计

`mcp_server` 通过 `server.py` 暴露知识库能力给 AI Agent。

### 只读工具

默认开放：

```text
kb.search
kb.get_document
kb.get_chunk
kb.get_metadata
kb.list_documents
kb.list_tags
kb.list_data_tables
kb.read_data_table
kb.query_data
kb.suggest_metadata
kb.preview_metadata_update
kb.apply_metadata_update
kb.bulk_preview_metadata_update
kb.bulk_apply_metadata_update
kb.create_app
kb.list_apps
kb.warmup
kb.warmup_status
```

其中查询类工具只读取索引、text 文本和元数据；metadata 更新类工具只修改 `store\docs.csv` 中的人工维护字段，并且必须遵循"先 preview、再 apply"的确认流程。`kb.warmup` 用于在执行 `kb.search` 等开销较大的工具前预热 Tantivy、LanceDB、jieba 和 Ollama embedding 模型，`kb.warmup_status` 返回最近一次预热的耗时分解，详见"冷启动与预热"一节。

#### 数据类工具（DuckDB 只读）

`kb.list_data_tables`、`kb.read_data_table`、`kb.query_data` 三件套服务于「数据类 + `data` 标签」的文档，底层为 `data\` 目录下的 Parquet 文件 + `db.sqlite` 中的 `data_tables` 注册表：

- `kb.list_data_tables(doc_id?)`：列出所有已登记的数据表，返回 `table_name / doc_id / source_path / sheet / parquet_path / columns / row_count`。
- `kb.read_data_table(table_name, limit=50, offset=0)`：按表名分页读取，最大 `limit=1000`。
- `kb.query_data(sql, limit=1000)`：在内存 DuckDB 上执行只读 SQL（每个登记表自动暴露为同名 View，背靠 `read_parquet(...)`）。安全约束：只允许单条 `SELECT` / `WITH`，禁用 `INSERT / UPDATE / DELETE / DROP / CREATE / ALTER / ATTACH / DETACH / COPY / EXPORT / IMPORT / PRAGMA / SET / LOAD / INSTALL / VACUUM / CHECKPOINT / TRUNCATE / CALL / MERGE` 等关键词；用户未指定 `LIMIT` 时自动套外层 `LIMIT`。

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

这些工具会修改 `store\docs.csv`、`text\` 或 `store\`，建议通过 `config.yaml` 中的 `mcp.enable_maintenance_tools` 控制，默认关闭。

### 冷启动与预热

`mcp_server` 自身启动很快，但**第一次** `kb.search` / `kb.get_chunk` 会触发以下一次性开销：

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

## H5 离线应用托管（apps\）

知识库提供一个 `apps\` 目录，用于存放由 Agent 生成或人工放置的 HTML5 离线应用。后台用 [miniserve](https://github.com/svenstaro/miniserve) 在 loopback 暴露本地静态站点。当前 `actions\start-apps-server.ps1` 默认以 `%USERPROFILE%\knowledges\` 为站点根目录，因此应用默认访问路径是 `http://127.0.0.1:8788/apps/<slug>/`。

### 目录与 URL 映射

```text
%USERPROFILE%\knowledges\apps\
  todo\
    index.html
    app.js
  network-map\
    index.html
    style.css
    data\
      nodes.json
```

对应地址：

```text
http://127.0.0.1:8788/apps/todo/
http://127.0.0.1:8788/apps/network-map/
```

H5 应用本身是纯前端的，加载完成后不依赖 miniserve；如需 Service Worker 真离线缓存，由 Agent 在生成时一并写入。

### 准备 miniserve

仓库提供 `actions\download-miniserve.ps1`，从 [svenstaro/miniserve releases](https://github.com/svenstaro/miniserve/releases) 拉取 Windows x86_64 单文件二进制并放到 `<repo>\vendor\miniserve.exe`，与 `.venv\` / `models\` 同处一地：

```powershell
# 默认版本
.\actions\download-miniserve.ps1

# 指定版本或自定义 URL
.\actions\download-miniserve.ps1 -Version 0.29.0
.\actions\download-miniserve.ps1 -Url "https://.../miniserve-x.y.z-x86_64-pc-windows-msvc.exe"

# 强制重下
.\actions\download-miniserve.ps1 -Force
```

### 启动静态服务

```powershell
.\actions\start-apps-server.ps1
# 默认监听 127.0.0.1:8788，根目录为 %USERPROFILE%\knowledges\
```

可通过参数或 `config.yaml` 覆盖：

```powershell
.\actions\start-apps-server.ps1 -Port 9000
.\actions\start-apps-server.ps1 -Interface 0.0.0.0   # 暴露到局域网（请确认信任环境）
.\actions\start-apps-server.ps1 -Root "D:\shared\knowledges"
```

```yaml
apps:
  host: "127.0.0.1"     # 默认 loopback；改 0.0.0.0 才会被外部访问
  port: 8788
  # 若保持 null，kb.create_app/kb.list_apps 会按 paths.apps_dir 自动补路径前缀；
  # 默认即 http://127.0.0.1:8788/apps/<slug>/。
  # 如你的 miniserve 根目录不是 knowledge_base_root，可显式设置：
  # "http://127.0.0.1:8788/apps"（或你的实际反向代理入口）
  base_url: null
```

> miniserve 是单二进制、零配置静态服务器，自带常见 MIME 表，支持 `--index index.html` 默认入口、目录浏览、`--hide-version-footer` 等开关。本仓库 `start-apps-server.ps1` 已带上推荐参数。

### MCP 工具：`kb.create_app` / `kb.list_apps`

| 工具 | 输入 | 行为 |
|---|---|---|
| `kb.create_app` | `slug`、`files: {relpath: content}`、`overwrite=false` | 在 `apps\<slug>\` 下写入文件，返回 `{slug, path, url, written_files, has_index, overwritten}` |
| `kb.list_apps` | 无 | 列出 `apps\` 下符合 slug 规则的子目录，返回 `slug / url / path / has_index / file_count / size_bytes` |

安全约束（实现位于 [services/apps.py](services/apps.py)）：

1. `slug` 必须匹配 `^[a-z0-9][a-z0-9-]{0,63}$`，禁止 `..` 与大小写混用；
2. 每个 `relpath` 仅允许 `[A-Za-z0-9._-]` 段、最大深度 8 层、单段 64 字符；
3. 写入前用 `Path.resolve()` 二次校验，确保所有目标仍在 `apps\<slug>\` 内；
4. 默认拒绝覆盖已存在的 app，需显式传 `overwrite=true`；
5. `kb.list_apps` 只列出符合 slug 规则的子目录，跳过用户手工放置的其它文件夹。

调用示例：

```json
{
  "name": "kb.create_app",
  "arguments": {
    "slug": "todo",
    "files": {
      "index.html": "<!doctype html><meta charset=utf-8><title>Todo</title><div id=app></div><script src=app.js></script>",
      "app.js": "// ...front-end code...\n"
    }
  }
}
```

返回示例：

```json
{
  "slug": "todo",
  "path": "C:\\Users\\huil\\knowledges\\apps\\todo",
  "url": "http://127.0.0.1:8788/apps/todo/",
  "written_files": ["app.js", "index.html"],
  "has_index": true,
  "overwritten": false
}
```

> 注意：上例 URL 来自 `apps.base_url`。当 `apps.base_url = null` 时，MCP 会按 `paths.apps_dir` 自动拼接默认前缀（默认结果：`http://{host}:{port}/apps/<slug>/`）；只有在你把静态站点根目录改到其它位置时，才需要手动设置 `apps.base_url` 覆盖。

Agent 收到结果后，应在 chat 中给出一句类似：

```markdown
已创建 **Todo App**：http://127.0.0.1:8788/apps/todo/ （需先运行 `actions\start-apps-server.ps1`）
```

### 与流水线的关系

`apps\` 不在 `docs\` 范围内：`tools.scan` / `convert` / `ingest` 不会扫描或索引它，全文/向量检索与 H5 应用互不干扰。如未来希望让 H5 内容也可检索，应单独评估方案，而不是把它放进 `docs\`。

## Agent 驱动的 metadata 维护

本项目不单独提供 HTML5 管理界面。metadata 维护采用 Agent-driven UI 模式：

```text
用户
  ↓ 在 Agent 界面中查看表格、建议和确认项
AI Agent
  ↓ 调用 MCP preview 工具
mcp_server
  ↓ 返回待修改内容
AI Agent
  ↓ 用户确认后调用 MCP apply 工具
store\docs.csv
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
  "docs": [
    {
      "id": "kb-001",
      "title": "项目A方案",
      "source_path": "docs\\project-a\\proposal.docx",
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

根据文件路径、标题、已有标签、text 内容摘要和现有标签集，建议标题、类型、标签、密级或状态。

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
      "source_path": "docs\\project-a\\proposal.docx",
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

但通常需要重新执行 `ingest.py` 或局部刷新索引元数据，以确保 Tantivy 和 LanceDB 中的 metadata 与 `store\docs.csv` 保持一致。

## 增量更新规则

### 原始文档发生变化

```text
用户修改 docs\ 中的文件
  ↓
执行 tools\scan.py
  ↓
store\docs.csv 中的 source_hash 变化
  ↓
执行 tools\convert.py
  ↓
text\ 和 store\db.sqlite 更新
  ↓
执行 tools\ingest.py
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

## text\ 路径规则

`text\` 应尽量镜像 `docs\` 的目录结构，方便追溯原文。

示例：

```text
docs\projects\a\proposal.docx
text\projects\a\proposal.md

docs\meetings\2026-05-21-review.pptx
text\meetings\2026-05-21-review.md

docs\data\customers.xlsx
text\data\customers.md
```

## AI Agent 使用规则

AI Agent 使用知识库时应遵守以下规则：

1. 通过 MCP 工具访问知识库，不直接操作 `%USERPROFILE%\knowledges\`。
2. 优先组合查询 `store\fulltext\` 和 `store\vector\`。
3. 对精确词、短语、编号、术语、文件名，优先使用全文检索。
4. 对概念、主题、相似内容、自然语言问题，优先使用向量检索。
5. 需要读取正文时，优先读取 `text\` 中的 Markdown。
6. 需要确认原始格式或附件时，再引用 `docs\` 中的源文件。
7. 回答基于知识库的问题时，必须给出来源路径。
8. 不得修改、删除或移动 `docs\` 中的原始文档。
9. 不得将 `confidentiality = confidential` 或 `private` 的内容发送到外部系统。
10. 检索结果不足时，应说明未找到充分依据，不得编造。

建议给 Agent 的提示词：

```text
你可以通过本地 `mcp_server` 使用本地知识库。
优先调用 kb.search 组合使用全文索引和向量索引。
精确关键词、短语、编号和文件名使用全文检索。
概念性、主题性和相似内容查询使用向量检索。
读取内容时优先使用 text\ 中的 Markdown。
如需确认原文，再引用 docs\ 中的原始文件。
回答时必须列出来源路径。
不要修改 docs\ 中的任何原始文件。
如果没有找到依据，请明确说明未找到相关资料。
```

## 环境初始化

在仓库根目录中执行。

首次初始化（按顺序执行，第 0–3 步每个新环境只做一次）。所有需要长脚本的步骤都封装在 `actions\` 里，且全部资源（embedding 模型由 Ollama 管理外，OCR / ASR 模型与 ffmpeg 二进制）都落在仓库内部，与 mcp_server runtime 同处一地：

```powershell
# 0) 安装本仓库及全部依赖到当前 .venv
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .

# 1) 拉取默认 embedding 模型（向量索引使用，由本机 Ollama 管理）
ollama pull qwen3-embedding:0.6b

# 2) 预加载 RapidOCR 模型（图片 OCR）
#    读取 config.yaml -> text_pipeline 的 image_ocr_* 选项，
#    首次执行会下载 PP-OCRv5 mobile 的 det/cls/rec 三个 ONNX 模型到
#    .venv\Lib\site-packages\rapidocr\models\，共约 21 MB。
python -m services.text_pipeline image

# 3) 预加载 SenseVoice + ffmpeg（音频/视频转写）
#    3a) 下载并解压 SenseVoice-Small ONNX 到 <repo>\models\sense-voice\
#        （需要 model.int8.onnx + tokens.txt；约 240 MB）。
.\actions\download-sense-voice.ps1

#    3b) 一次性构造 sherpa-onnx OfflineRecognizer 并定位 ffmpeg 二进制
#        （ffmpeg 由 imageio-ffmpeg 随 pip 自动带入 .venv，无需额外安装）。
python -m services.text_pipeline audio

# 也可以把 image + audio 合并成一条命令（任一失败不影响另一项预热）：
# python -m services.text_pipeline all

# 4) 跑一次完整流水线
python -m tools.scan
python -m tools.convert
python -m tools.ingest

# 5) （可选）下载 miniserve，准备 H5 离线应用托管
#     落到 <repo>\vendor\miniserve.exe，与 .venv / models 同处一地。
.\actions\download-miniserve.ps1
# 之后用 .\actions\start-apps-server.ps1 启动
# 默认访问路径示例： http://127.0.0.1:8788/apps/<slug>/
```

> 第 2、3 步若 `docs\` 里不会出现对应类型的文件，可以跳过——不会影响其余步骤。
> 之后的日常使用只需要执行第 4 步即可；只有当 RapidOCR / SenseVoice 模型目录被清理后，才需要重跑第 2 / 3 步重新拉模型。
> `actions\download-sense-voice.ps1` 支持 `-Force` 强制重下、`-Url` 指定其它发布包；`python -m services.text_pipeline` 支持 `image|audio|all` 三种预热目标。

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

启动 `mcp_server`：

```powershell
python .\server.py
```

用 MCP Inspector 调试本地 `mcp_server`：

```powershell
npx @modelcontextprotocol/inspector .\.venv\Scripts\python.exe -m server
```

如果只修改了标签、标题、密级等人工语义字段，通常不需要重新转换原始文档，但需要重新执行 `ingest.py`，以便全文索引和向量索引中的元数据保持一致。

> **重要**：本项目所有 Python 代码（`tools\*.py`、`server.py`、`hf` 等）都必须在当前项目的 `.venv` 虚拟环境中运行，不要使用全局 Python。每次新开终端先执行 `.\.venv\Scripts\Activate.ps1` 激活环境，确认提示符前出现 `(.venv)` 后再运行任何命令；AI Agent 协助执行命令时也应遵循同样规则，避免污染全局环境或引入版本冲突。

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

1. `docs\` 和 `store\docs.csv` 是最重要的持久状态。
2. `text\` 可以由 `convert.py` 重建。
3. `store\db.sqlite` 可以由 `store\docs.csv` 和 `text\` 重建。
4. `store\fulltext\` 可以由 `ingest.py` 重建。
5. `store\vector\` 可以由 `ingest.py` 重建。
6. 如果索引损坏，应优先重建索引，而不是修改原始文档。

## 维护建议

1. 原始文档统一放入 `%USERPROFILE%\knowledges\docs\`，不要直接放入 `text\`。
2. 修改原始文档后，先执行 `scan.py`。
3. 人工标签、分类、密级建议维护在 `store\docs.csv` 中。
4. 批量导入新文档后，先运行 `scan.py`，确认 `store\docs.csv` 中新增记录正确，再运行 `convert.py` 和 `ingest.py`。
5. 定期备份 `docs\` 和 `store\docs.csv`。
6. `text\`、`store\fulltext\` 和 `store\vector\` 可以备份，但应视为可重建产物。
7. 代码仓库应单独进行版本管理，避免与知识库数据备份混在一起。
