# Changelog

格式参照 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/),
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。
生产发布流程见 [docs/release-guide.md](docs/release-guide.md)。

## [Unreleased]

### Added

- **FTS5 BM25 关键词双通道检索**(schema v13): L1 检索在向量 KNN 之外
  新增 BM25 关键词通道,RRF(k=60)融合——修复专有名词/日期等精确词漏检,
  LoCoMo 采样 R@10 较裸向量 +10pp
- **Cross-Encoder 精排**(可选 `[rerank]` extra,默认 noop):
  RRF 融合后对 L1/L2 候选按层精排(bge-reranker-v2-m3,每层上限 50 条
  候选保护延迟);全量 1536 题 R@10 95.3% → 97.4%
- **写入时 LLM 事实抽取**(可选 `[llm]` extra,默认 PassThrough,
  core 保持零 LLM): `FactExtractor` Protocol + `LLMFactExtractor`,
  长块拆原子事实逐条入库,失败降级回原文;抽取模式下事实可跳过 L0
  直送 L1(`facts_bypass_l0`)
- Benchmark 工具链四套: LongMemEval / LoCoMo(自家严口径)、
  retrieval_baseline(纯检索 R@10,无需 LLM key)、mem0_compat
  (mem0 官方协议对齐口径,含离线 rejudge 工具)
- 服务化配置支持 `reranker` / `fact_extractor` 开关(config.toml)
- CI: lint + Python 3.11-3.14 测试矩阵;发布流水线增加测试门禁、
  tag/版本一致性校验、provenance attestation
- 文档: as-built 架构总览(含名词解释表)、README 成绩速览

### Fixed

- promotion 时间锚定: `time_start` 取 `time_range.exact > approx_start >
  created_at`(此前一律用墙钟 created_at,历史对话灌入后真实时间丢失)
- embedder 对 `<|endoftext|>` 等特殊 token 字面量的净化
  (sentence-transformers v6 丢弃 tokenizer_kwargs 后的兼容修复)

### 记忆质量成绩(mem0 兼容口径,glm-5.3-flash 作答+判卷)

- LongMemEval: 96.7%(mem0 官方 94.4,gpt-5)
- LoCoMo: 98.9%(mem0 官方 92.5,gpt-5)

## [0.1.1] - 2026-08-28

- MIT License + PyPI 发布 workflow(Trusted Publishing/OIDC)
- `requires-python >=3.11`

## [0.1.0] - 2026-08

首个发布版本: MVP 全量(Layer 0-5)。

- 数据模型(FuzzyMemory/图谱/因果)+ SQLite 存储引擎
  (sqlite-vec 向量 / R-tree 空间 / 递归 CTE 图查询 / 时态查询)
- L0 工作记忆(LRU + 快照)、Token Budget 上下文构建、L0→L1 晋升
- 三重触发调度器 + GRACEFUL 抢占断点续跑;5 类核心任务
  (整合/遗忘/摘要/因果/语义);项目冷启动 bootstrap
- Layer 4 锁管理器 + 冲突检测;Layer 5 矛盾检测 / 归档冷热分层 /
  跨项目共现提升 global
- MCP server + Web 面板 + 三平台自启动注册脚本
