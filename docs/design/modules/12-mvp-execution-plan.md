# MVP 执行计划(WBS)

> **版本**: v1.1
> **更新日期**: 2026-08-21
> **配套文档**: [agent-memory-design.md §14.2](../agent-memory-design.md) MVP 范围
> **目的**: 把 8 人周 MVP 拆成 35+ 原子任务,锁定依赖、周交付、风险、验收

---

## 0. 状态总览

| 模块 | 人周 | 状态 |
|------|------|------|
| Layer 0 数据模型 | 1 | ✅ 完成(2026-06-21) |
| Layer 1 存储引擎 | 3 | ✅ 完成(2026-08-21) |
| Layer 2 生命周期 | 2 | ✅ 完成(2026-08-21) |
| Layer 3 调度+冷启动 | 1 | ✅ 完成(2026-08-21) |
| MemoryMiddleware | 1 | ✅ 完成(2026-08-21) |
| **MVP 合计** | **8** | **8/8 完成** |
| P0 任务调度+抢占+5 类核心任务 | 4 | ✅ 完成(2026-08-21,P0-a 调度器/抢占 + P0-b 核心任务) |
| P1-a Layer 4 并发控制(锁+冲突检测+解决) | 2 | ✅ 完成(2026-08-21,484 tests) |
| P2-a 跨项目自动提升 + 跨项目克隆 | 2 | ✅ 完成(2026-08-21,527 tests,提升准确率实测 100%) |
| P2-b 历史归档 + 性能优化(向量量化 + predicate 编码) | 1 | ✅ 完成(2026-08-21,585 tests,§14.5 两条退出标准实测达标) |

---

## 1. WBS 任务分解

### 1.1 Layer 0 数据模型 ✅

| ID | 任务 | 人天 | 状态 | 备注 |
|----|------|------|------|------|
| L0.1 | 6 StrEnum + 工具类(ids/timeutil) | 1 | ✅ | enums.py + utils/ |
| L0.2 | 7 dataclass + 序列化(to_dict/from_dict) | 2 | ✅ | fuzzy.py + graph.py + scope.py |
| L0.3 | msgpack 序列化(datetime Ext) | 1 | ✅ | serialization.py |
| L0.4 | 单元测试(58 个用例) | 2 | ✅ | tests/unit/ |

**实际花费**: 5 人天(预计 6 人天)— 进度正常

---

### 1.2 Layer 1 存储引擎(15 人天)

| ID | 任务 | 人天 | 依赖 | 状态 |
|----|------|------|------|------|
| L1.1 | 融合 DDL(主文档字段 + 模块文档语法) — 9 张表 + 2 虚拟表 | 2 | — | ✅ 2026-06-21 |
| L1.2 | DDL — `project_current_state` + 触发器(§6.3) | 1 | L1.1 | ✅ 合并到 L1.1 |
| L1.3 | DDL — `memory_l0_snapshot` 表 | 0.3 | L1.1 | ✅ 合并到 L1.1 |
| L1.4 | DDL — sqlite-vec 虚拟表(`memory_vectors`) | 0.5 | L1.1 | ✅ 合并到 L1.1 |
| L1.5 | DDL — R-tree 虚拟表(`memory_locations_rtree`) | 0.5 | L1.1 | ✅ 合并到 L1.1 |
| L1.6 | Schema 版本化(PRAGMA user_version + migrations) | 1 | L1.1-L1.5 | ✅ 合并到 L1.1 |
| L1.7 | SQLiteEngine(aiosqlite + WAL pragma + 扩展加载) | 1.5 | L1.6 | ✅ 2026-06-21 |
| L1.8 | 查询 — temporal(query_at_time / query_in_range) | 1 | L1.7 | ✅ 2026-06-21 |
| L1.9 | 查询 — spatial(query_in_area / query_in_location) | 1 | L1.7 | ✅ 2026-06-21 |
| L1.10 | 查询 — graph(find_path / find_n_degree_relations) | 1.5 | L1.7 | ✅ 2026-06-21 |
| L1.11 | 查询 — causal(trace_causal_chain) | 1 | L1.7 | ✅ 2026-06-21 |
| L1.12 | 查询 — scope filter 应用 | 0.5 | L1.8-L1.11 | ✅ 2026-06-21 |
| L1.13 | hybrid_search(RRF fusion) | 1.5 | L1.8-L1.11 | ✅ 2026-06-21 |
| L1.14 | StorageEngine 接口封装 | 1 | L1.7-L1.13 | ✅ 2026-06-21 |
| L1.15 | 集成测试 + benchmark(P99 < 50ms write) | 1 | L1.14 | ✅ 2026-08-21 |

**关键风险**:
- ~~DDL 字段冲突(主文档 §6.1 vs 模块文档 02)→ L1.1 必须先冻结字段~~ ✅ 已解决(见 L1.1 决策)
- sqlite-vec 与 aiosqlite 集成(同步扩展加载 + 异步查询)→ L1.7 可能需要 adapt

---

### 1.3 Layer 2 生命周期(10 人天)

| ID | 任务 | 人天 | 依赖 | 状态 |
|----|------|------|------|------|
| L2.1 | L0WorkingMemory(`LRUCache` per 决策 D4) | 1 | L0,L1.7 | ⏳ |
| L2.2 | L0 快照持久化(memory_l0_snapshot) | 1 | L1.3,L2.1 | ⏳ |
| L2.3 | **决策 D5**: Embedding 方案(维度/库/模型) | 0.5 | — | ✅ 2026-08-21 |
| L2.4 | Embedder 实现(sentence-transformers) | 1.5 | L2.3 | ✅ 2026-08-21 |
| L2.5 | L1 向量存储(sqlite-vec 适配 + Embedder) | 1.5 | L1.4,L2.4 | ✅ 2026-08-21 |
| L2.6 | TokenCounter(tiktoken cl100k_base) | 0.5 | L0 | ⏳ |
| L2.7 | ContextBuilder(Token Budget, §12.1) | 2 | L1.13,L2.5,L2.6 | ✅ 2026-08-21 |
| L2.8 | 晋升规则(L0→L1 over 800 tokens) | 1 | L2.1,L2.5 | ✅ 2026-08-21 |
| L2.9 | 集成测试(benchmark: recall P99 < 500ms) | 1 | L2.7,L2.8 | ✅ 2026-08-21 |

**关键风险**:
- Embedding 维度冲突(§6.4 写 `FLOAT[1536]`,BGE-M3 是 1024 维)→ **L2.3 必须先决策**

---

### 1.4 Layer 3 调度+冷启动(5 人天)

| ID | 任务 | 人天 | 依赖 | 状态 |
|----|------|------|------|------|
| L3.1 | ProjectBootstrap.initialize API | 1 | L1.14,L2.7 | ✅ 2026-08-21 |
| L3.2 | Onboarding wizard(5 个核心问题) | 1 | L3.1 | ✅ 2026-08-21 |
| L3.3 | 项目模板(web/cli/data/agent YAML) | 1.5 | L3.1 | ✅ 2026-08-21 |
| L3.4 | Active learner stub(无 LLM 版本) | 0.5 | L3.1 | ✅ 2026-08-21 |
| L3.5 | README 解析 + 实体提取(规则版) | 1 | L3.1 | ✅ 2026-08-21 |
| L3.6 | 集成测试(Top-5 召回率 > 60%) | 0.5 | L3.2-L3.5 | ✅ 2026-08-21(实测 8/8=100%,确定性关键词+图通道) |

**关键风险**:
- LLM provider 未定 → MVP 用规则提取兜底(见 EXECUTION_PLAN_GAPS.md §3.6)

---

### 1.5 MemoryMiddleware(5 人天)

| ID | 任务 | 人天 | 依赖 | 状态 |
|----|------|------|------|------|
| M.1 | DTO 定义(WriteRequest/Response + RecallRequest/Response + ProjectInit*) | 1 | L0 | ✅ 2026-08-21 |
| M.2 | `initialize_project()` 端到端 | 1 | L3.6,M.1 | ✅ 2026-08-21 |
| M.3 | `write()` 端到端 | 1 | L1.14,L2.8,M.1 | ✅ 2026-08-21 |
| M.4 | `recall()` 端到端 | 1 | L2.7,M.1 | ✅ 2026-08-21 |
| M.5 | close_session(L0 清理) | 0.3 | L2.1,M.1 | ✅ 2026-08-21 |
| M.6 | 端到端 E2E 测试(创建→写入→检索) | 0.7 | M.2-M.5 | ✅ 2026-08-21 |

---

## 2. 任务依赖图

```mermaid
gantt
    title MVP 执行甘特图(7 周剩余)
    dateFormat  YYYY-MM-DD
    axisFormat  W%U

    section Layer 1
    L1.1-1.5 DDL 编写          :l1ddl, 2026-06-22, 4d
    L1.6 Schema 版本化         :l1ver, after l1ddl, 1d
    L1.7 SQLiteEngine          :l1eng, after l1ver, 2d
    L1.8-1.11 查询函数         :l1qry, after l1eng, 5d
    L1.12 Scope filter         :l1scp, after l1qry, 1d
    L1.13 hybrid_search        :l1hyb, after l1scp, 2d
    L1.14 StorageEngine        :l1api, after l1hyb, 1d
    L1.15 Benchmark            :l1ben, after l1api, 1d

    section Layer 2
    L2.1 L0 cachebox           :l2l0, after l1eng, 1d
    L2.2 L0 快照持久化         :l2snp, after l2l0, 1d
    L2.3 D5 Embedding 决策     :l2d5, 2026-06-22, 1d
    L2.4 Embedder              :l2emb, after l2d5, 2d
    L2.5 L1 sqlite-vec         :l2l1, after l1.4,l2emb, 2d
    L2.6 TokenCounter          :l2tk, 2026-06-22, 1d
    L2.7 ContextBuilder        :l2cb, after l1hyb,l2l1,l2tk, 3d
    L2.8 晋升规则              :l2pr, after l2l0,l2l1, 1d
    L2.9 Benchmark             :l2bn, after l2cb, 1d

    section Layer 3
    L3.1 Bootstrap API         :l3b1, after l1api,l2cb, 1d
    L3.2 Wizard                :l3wz, after l3b1, 1d
    L3.3 Templates             :l3tpl, after l3b1, 2d
    L3.4 Active learner        :l3al, after l3b1, 1d
    L3.5 README 解析           :l3rm, after l3b1, 1d
    L3.6 召回率测试            :l3rt, after l3wz,l3tpl,l3al,l3rm, 1d

    section Middleware
    M.1 DTO 定义               :m1dto, 2026-06-22, 1d
    M.2 initialize_project     :m2init, after l3rt,m1dto, 1d
    M.3 write                  :m3wr, after l1api,l2pr,m1dto, 1d
    M.4 recall                 :m4rc, after l2cb,m1dto, 1d
    M.5 close_session          :m5cls, after l2l0,m1dto, 1d
    M.6 E2E                    :m6e2e, after m2init,m3wr,m4rc,m5cls, 1d
```

---

## 3. 周交付物(剩余 7 周)

| 周 | 模块 | 交付物 | 退出标准 |
|---|------|--------|---------|
| **W1** | Layer 1 (Part 1) | DDL + Schema 版本化 + SQLiteEngine | `python -c "from smilex.memory.storage import SQLiteEngine; ..."` 可创建带 7+2 表的 .db |
| **W2** | Layer 1 (Part 2) | 6 类查询函数 + hybrid_search + StorageEngine API + benchmark | `query_at_time` P99 < 5ms,`hybrid_search` P99 < 200ms |
| **W3** | Layer 2 (Part 1) | L0 cachebox + Embedder + L1 sqlite-vec 集成 | L0 hit rate > 70%,Embedding 1024 维稳定输出 |
| **W4** | Layer 2 (Part 2) | ContextBuilder + 晋升规则 + benchmark | recall P99 < 500ms,Token Budget 裁剪准确 |
| **W5** | Layer 3 | ProjectBootstrap + Wizard + Templates + Active learner | 新项目冷启动 < 5 分钟,Top-5 召回率 > 60% |
| **W6** | MemoryMiddleware | DTO + initialize_project + write + recall + close_session | E2E 测试通过 |
| **W7** | 硬化 + 打包 | SLA 验收 + benchmark 完善 + PyPI 打包 | `pip install smilex-memory` 可用 |

---

## 4. 风险与应对 Playbook

### 4.1 阻塞性风险(实施前必须解决)

| 风险 ID | 描述 | 影响 | 应对 | 决策点 |
|---------|------|------|------|--------|
| R1 | sqlite-vec + aiosqlite 异步集成 | Layer 1 全部阻塞 | L1.7 阶段实测,若不行 fallback 到 sqlite3 同步 + run_in_executor | W1 |
| R2 | Embedding 维度冲突(§6.4 写 1536 vs BGE-M3 1024) | Layer 2 阻塞 | **决策 D5**(L2.3 任务)统一为 1024 维,文档同步修正 | ✅ 已解决(2026-08-21,主文档 §6.4 已改为 FLOAT[1024]) |
| R3 | cachebox API 已漂移(决策 D4 解决) | Layer 2 L0 实施 | 已决策:用 LRUCache,应用层清理 | ✅ 解决 |

### 4.2 缓冲性风险(实施中发现)

| 风险 ID | 描述 | 应对 |
|---------|------|------|
| R4 | DDL 字段命名冲突(主文档 vs 模块文档) | L1.1 阶段以主文档 §6.1 字段为准,所有命名冲突列在 PR review |
| R5 | LLM provider 未定(L3.4) | MVP 用规则提取兜底,P1 接入 OpenAI 兼容 API |
| R6 | 召回率测试缺标注数据集 | L3.6 阶段手工标 5-10 个 golden set |
| R7 | SLA P99 < 50ms 写入可能不达标(SQLite 单写者) | 先记录数据,L1.15 benchmark 后决定是否需要写入批量化(§15.2) |

### 4.3 兜底回退

| 决策点 | 主方案 | Fallback |
|--------|--------|----------|
| sqlite-vec 异步 | aiosqlite + sqlite_vec | sqlite3 同步 + run_in_executor |
| Embedding | sentence-transformers + BGE-M3 | OpenAI 兼容 API(text-embedding-3-small) |
| 主动学习 LLM | 规则提取(零依赖) | 推迟到 P1 |
| 召回率测试 | 5-10 个 golden set | 推迟到 P0 |

---

## 5. 验收 Checklist(对照 §14.2 退出标准)

### 5.1 pip install smilex-memory 可用 ✅

- [x] pyproject.toml 完整 metadata(classifiers, license, urls)— license=MIT + authors/keywords/classifiers + urls 指向 git remote(2026-08-21)
- [x] `uv build` 生成 wheel — wheel + sdist 均生成成功(2026-08-21)
- [x] `pip install dist/smilex_ai_memory-*.whl` 成功 — 临时干净 venv(`uv venv` + `uv pip install`)安装成功(2026-08-21)
- [x] 安装后 `python -c "import smilex; print(smilex.__version__)"` 正常 — 输出 0.1.0,wheel 含模板 YAML(4 个)与 schema SQL(10 个)
- [x] README.md 快速开始验证 — 命令实测通过，结构树已补充新模块(2026-08-21)

### 5.2 新项目 < 5 分钟可用 ✅

- [x] `MemoryMiddleware.initialize_project(name, desc, tech_stack)` 端到端 — E2E 测试覆盖(M.6)
- [x] 冷启动流程 6 步全部完成(向导 + 模板 + 主动学习)— L3.1-L3.5 完成
- [x] 实测:在干净的 Python 环境从零到首次 recall < 5 分钟 — 实测 engine 初始化 + initialize_project ≈ 13ms,首次 recall ≈ 0.03ms(2026-08-21)

### 5.3 写入 P99 < 50ms ✅

- [x] L1.15 benchmark 脚本 `tests/benchmarks/write_latency.py`
- [x] 1000 次写入采样,P99 < 50ms(单写者,无并发)— 实测 P99 ≈ 2.9ms,P50 ≈ 0.20ms(2026-08-21 复测)
- [ ] 若不达标:启用 WAL pragmas + 写入批量化

### 5.4 检索 P99 < 500ms ✅

- [x] L2.9 benchmark 脚本 `tests/benchmarks/recall_latency.py`
- [x] 100 次混合检索采样,P99 < 500ms(token budget 4000)— 实测 P99 ≈ 6.6ms,P50 ≈ 5.7ms(2026-08-21 复测)
- [ ] 若不达标:检查 sqlite-vec HNSW 参数 / ContextBuilder 算法

### 5.5 冷启动 Top-5 召回率 > 60% ✅

- [x] L3.6 golden set: 1 个标准 web 项目(ShopAPI)+ 8 个标准 query(tests/unit/test_bootstrap_recall.py)— 2026-08-21
- [x] 每个标准 query 检查 Top-5 是否包含人工标注的应召回记忆
- [x] 召回率 = (命中数 / 应召回数) > 60% — 实测 8/8 = 100%(确定性关键词+图通道;语义通道待 BGE-M3 接入后叠加)
- [ ] 扩充到 5-10 个典型项目(P1,配合批量导入)

---

## 6. 文档同步任务

每完成一个 Layer,需同步更新:

| 触发 | 更新内容 | 状态 |
|------|---------|------|
| L1.1 完成 | DDL 冻结后,更新主文档 §6.1 + 模块文档 02 | ✅ |
| L1.4 完成 | 主文档 §6.4 向量 DDL 与实际一致 | ✅ |
| L2.3 决策 D5 | 主文档 §6.4 维度 1536 → 1024,§19.1 embedding 行更新 | ✅ 2026-08-21 |
| L2.1 完成 | 主文档 §7.2 LRUCache 代码示例(取代 TTLPrefixCache) | ✅ 2026-08-21 |
| L2.4 完成 | 主文档 §19.1 增加 sentence-transformers 依赖 | ✅ 2026-08-21(可选 `embedding` extra) |
| M.6 完成 | 主文档 §14.2 退出标准 checklist 标注 ✅ | ✅ 2026-08-21(含实测值) |

---

## 7. 决策记录(本计划新增)

### D5: Embedding 方案(已决策 2026-08-21,L2.3)

**决策**: 候选 A 变体 — Protocol 抽象 + 双后端

- **维度**: 统一 **1024**(BGE-M3;主文档 §6.4 的 `FLOAT[1536]` 废弃,schema `010_virtual_tables.sql` 已按 1024 建表)
- **架构**: `Embedder` Protocol(`embed` / `embed_batch` / `dimension`),实现于 `src/smilex/memory/lifecycle/embedder.py`
- **默认后端**: `HashEmbedder`(零依赖,SHA-256 分块 → 1024 维 + L2 归一化)——只保证确定性,不保证语义相似度,用于 MVP 默认/测试/无模型环境
- **可选后端**: `SentenceTransformerEmbedder`(BGE-M3,懒加载),通过 `[project.optional-dependencies]` 的 `embedding` extra 安装,不进主依赖
- **工厂**: `get_embedder(EmbedderConfig(backend="hash"|"sentence-transformers"))`,默认返回 `HashEmbedder`

**理由**: 符合「嵌入式优先」原则(§1.2.3),无 API 成本;Hash 默认保证 MVP 全流程在无模型环境可跑通,语义质量可后续无缝切换到 BGE-M3

---

## 8. 下一会话起点

**P2 已全部完成并验收**(2026-08-21:P2-a 跨项目自动提升 + 跨项目克隆;P2-b 历史归档 + 性能优化,585 tests 通过,§14.5 三条退出标准全部实测达标,见主文档 §14.5)。

**2026-09-08 状态刷新**(原"下一阶段进入 P3 演进路径或产品化硬化"已完成或降级):
- 产品化硬化 ✅: §15.3 可观测性四件套 + §15.4 安全(API Key/SQLCipher/PII)已落地
- 记忆主动优化三期 ✅: 检索反馈闭环/近重复合并 → superseded/容量治理/删除守卫 → 错误指纹/教训闭环
- P3 演进路径第一项 ✅: LLM 摘要压缩(summarize 任务可选 LLM 后端)
- 服务化出口 ✅: memory_recall entity/time 聚焦参数 + memory_graph_query 工具(L2 图谱/时序策略接出)
- 快照迁移路径 ✅: 克隆/导入向量重建 + merge 归并 + locations 对称化 + 包格式 1.1(08/11 号文档同步修订)
- 设计文档治理 ✅: 10 号 v2 重写(现有原语)、08/11 减法修订、ADR-015~018 回填主文档、演进路径逐条降级/关闭(02/03/04/05 §5)、GAPS D6 关闭为 wontfix

**剩余可选项**(按价值排序,均为"按需再做"而非待办):
1. 社区检测 Louvain 轻量版(08 号修订记录 — semantic 任务从连通分量升级,networkx 内置 + 固定 seed + Summarizer Protocol + VectorStore 写向量)
2. 个人成长模块 Phase 1(10 号 v2 — 0.5 天约定层: MCP instructions 写入约定 + skill_level 三元组约定)
3. 快照含归档表开关 + 导出的 CLI/MCP 用户入口(11 号修订记录)
4. PG 适配器/分布式锁等(02/05 §5 — 被多用户需求阻塞,远期)

**P2 范围**(主文档 §14.5 / 06-layer5 §5):
- Layer 5 跨项目自动提升(ScopePromoter,SQL GROUP BY 共现检测)— ✅ P2-a 完成(2026-08-21)
- Layer 3 跨项目克隆(CloneFilter/CloneResult,GAPS §3.4 待补)— ✅ P2-a 完成(2026-08-21)
- Layer 5 历史归档(冷热分层)— ✅ P2-b 完成(2026-08-21,同库归档表方案)
- 性能优化(向量量化 int8 + predicate 字典编码)— ✅ P2-b 完成(2026-08-21,取舍见下)

**P2-b 进展**(2026-08-21):
- 新增 schema 迁移 `011_archive_and_predicate_dict.sql`(SCHEMA_VERSION 10 → 11):`triples_archive` / `temporal_fragments_archive`(热表同构 + archived_at)+ `predicate_dict` 字典表 + `triples.predicate_code` 冗余编码列(存量回填)
- **修复**: schema loader 与 SQLiteEngine 的 010 跳过逻辑 — 无 sqlite-vec 环境建库(user_version 越过 10 但虚拟表缺失)后,带扩展重开按表存在性补建 010,不回退 user_version(`tests/unit/test_schema.py::test_vec_tables_backfilled_after_skip` 固化)
- 新增 `src/smilex/memory/quality/archiver.py`:`RetentionPolicy`(§11.6:历史三元组 365 天 / 片段 180 天 + 低重要度 0.3,当前状态三元组永不归档,实体不归档)+ `Archiver`(find_cold 预览 / archive_expired 归档 / query_archived 非向量召回 / restore 回迁 / run 调度入口)+ `register_archive_task`(复用 P0 调度器,LOW 优先级可中断)。归档动作:清向量(vector_links + memory_vectors,外键要求)→ INSERT OR IGNORE 入归档表(triples 同步补 predicate_code)→ 删热表,每批一个事务;幂等 + 断点续传(cursor=批尾 id,state.phase);三元组回迁临时摘除 trg_update_current_state 触发器,防历史 project_state 触发 LWW 污染 project_current_state
- **偏离说明**: 06-layer5 §3.5 的 ATTACH 独立归档库简化为同库归档表(嵌入式单库,召回路径最简单,无需管理第二文件);§11.6 的因果链压缩/矛盾记录归档未做(数据量小,收益低)
- 新增 `src/smilex/memory/quality/quantization.py`:int8 标量量化编解码(06-layer5 §3.6 算法 1,逐向量 min-max → uint8,4x 压缩,往返误差 ≤ (vmax-vmin)/510)。**取舍**: vec0 是 FLOAT[1024] 无法原位存 int8,改列类型/影子表双写风险大于收益 — 热向量保持 float32 保 KNN 正确性,实际体积优化由归档清向量承担;量化工具经往返测试就绪,生产应用留待后续(PQ/辅助向量副本)
- 新增 `src/smilex/memory/storage/predicate_codec.py` + `StorageEngine.write_triple` 写入编码。**取舍**: 读取路径仍走 predicate TEXT(不重写全部查询为 JOIN 字典,保护 527 测试基线),字典编码收益落在归档表与后续统计;裸 SQL 写入路径(如 ScopePromoter)允许 code 为 NULL,归档时补齐
- middleware 薄封装(向后兼容): `recall(..., include_archived=False)`(True 时经 query_archived 并入归档结果,`[归档]` 前缀,遵循 scope 缺省规则)+ `archive_expired(policy, scope)` + `restore_archived(table, ids, scope)`
- **退出标准实测**: 「10 项目/年存储 < 3 GB」→ `tests/benchmarks/storage_size.py` 抽样外推(1 项目 × 1 月实测 ×120):未优化 1.18 GiB / 优化后 1.17 GiB,PASS;「归档数据可按需召回」→ `tests/unit/test_archiver.py`(include_archived 召回 / restore 回迁 / 幂等 / 抢占续传 12 用例)。**实测发现**: sqlite-vec vec0 按 chunk 存向量(约 1000 条/chunk),碎片化删除不释放未清空 chunk,归档清向量的体积收益需成批归档 + VACUUM 才体现
- `uv run pytest tests/ -q` 585 passed(新增 17:test_archiver.py 12 + test_quantization.py 5;test_schema/test_sqlite_engine 各更新 1 处硬编码版本断言);`uv run ruff check src/ tests/` 通过;benchmarks: write P99 3.1ms / recall P99 8.0ms(无显著劣化)/ storage_size PASS

**P2-a 进展**(2026-08-21):
- 新增 `src/smilex/memory/quality/scope_promoter.py`:`ScopePromoter`(§11.3 规则版,零 LLM/零向量)。共现判定用归一化模式键 `(subject_key, predicate, object_key)` — subject/object 实体取归一化 entity_id(无实体行回退原始 id),字面值经 `normalize_value`(去首尾空白 + 折叠内部空白 + 小写)加 `value:` 前缀与实体引用区分;只统计 project scope 现行三元组(global/tenant 不计入),状态跟踪类 relation_type(project_state/task_status/config)默认排除。提升动作:global 副本 + 端点实体按 entity_id 去重补建,source_closet 回指源记录(三元组取 confidence 最高的代表性源),幂等按模式键查重。`manual_promote(triple_id)` 手动提升;`run(ctx, payload)` 符合 InterruptibleRun(每模式一检查点,cursor=模式键,断点续传),`register_scope_promotion_task(scheduler, storage)` 挂接调度器不改框架
- **偏离说明**: 06-layer5 §3.4 的 MIN_SUPPORT=5 / MIN_CONFIDENCE=0.8(置信度=1-反例比例)依赖反例统计,规则版无可靠反例定义,简化为 min_projects(默认 3)+ min_support(默认 1)双阈值;LLM 置信度留待语义层接入
- 新增 `src/smilex/memory/scheduler/bootstrap/cross_project_cloner.py`:`CloneFilter`(实体类型/片段层/时间 since-until/三类开关/current_only)+ `CloneResult`(GAPS §3.4 补全)+ `CrossProjectCloner`。`clone_to` 行级克隆(保留 confidence/certainty/valid_from/predecessor,与种子级 apply_seeds 互补不复用):ID 全部新 ULID,实体/三元组/片段引用经统一映射表改写(predecessor 两阶段改写与源行顺序解耦),端点被过滤的三元组跳过;溯源 source_closet 回指源记录;幂等(实体按 entity_id、三元组按全键、片段按 fragment_id 去重)。`export_package`/`import_package`:JSON 快照包(模块 11 模式 B 轻量版,不含向量 ADR-022,版本校验),导入复用克隆的重映射+去重写入路径
- middleware 集成(向后兼容新增):`MemoryMiddleware.clone_project(source_scope, target_name=None, *, clone_filter=None)`,新建 project scope 克隆,target_name 可选写入项目实体种子(复用 apply_seeds);bootstrap 延迟导入避免循环依赖
- **退出标准实测**: 「跨 3 项目共现自动提升准确率 > 90%」→ 24 条标注样本(12 正例 + 12 负例,覆盖大小写/空白变体、实体引用、裸 subject_id、中文、状态类排除、历史排除、global/tenant 不计数等)实测 24/24 = **100%**(`tests/unit/test_cross_project_promotion.py::test_promotion_accuracy_above_90_percent`)
- `uv run pytest tests/unit/ -q` 527 passed(新增 17:test_cross_project_promotion.py 8 + test_project_clone.py 9);`uv run ruff check src/ tests/` 通过

**P1 范围**:
- Layer 4 锁管理器 + 冲突检测 + 解决 — ✅ P1-a 完成(2026-08-21)
- Layer 3 批量导入(Git/Markdown)— ✅ P1-b 完成(2026-08-21)
- Layer 3 种子注入 — ✅ P1-b 完成(2026-08-21)
- Layer 5 矛盾检测(同步)— ✅ P1-b 完成(2026-08-21)

**P1-b 进展**(2026-08-21):
- 新增 `src/smilex/memory/scheduler/bootstrap/bulk_importer.py`:`ImportSource`/`ImportResult`/`ImportKind`(GAPS §3.4 补全)+ `BulkImporter`。git 导入用 CLI 子进程解析 `git log`(%x1e/%x1f 分隔 + --name-only,零新依赖,git 不可用抛 RuntimeError):提交 → L1 时序记忆(fragment_id="git:{repo}:{hash}" 幂等)+ 作者 person 实体 + 文件 object 实体 + contributes_to/contains_file 三元组;分批 + CheckpointStore 断点续传(cursor=批尾提交 hash,成功清理断点);markdown 目录批量(大文件按行分块提取,每文件一条 L1 记忆,fragment_id="md:{相对路径}");文本批次(RuleBasedExtractor + fragment_id="text:{sha256[:16]}")。实体/三元组统一走 apply_seeds 幂等写入 + 向量通道
- 新增 `src/smilex/memory/scheduler/bootstrap/seed_injector.py`:`SeedInjector.inject(scope, source)` 薄封装(不重复造轮子,持久化复用 apply_seeds),source 支持模板 key / 外部 YAML 种子包(与模板同构,load_seed_pack)/ ExtractionResult;模块级便捷函数 `inject_seeds`。注:§9.5.2 步骤 4 的「从 global 按技术栈召回经验」依赖语义检索,HashEmbedder 无语义相似度,留待 D5 语义 Embedder 接入
- 新增 `src/smilex/memory/quality/contradiction.py`(Layer 5 首个落地组件):`ContradictionDetector` 规则版四维同步检测(VALUE 枚举/单值谓词、NUMERIC 10% 容差、TEMPORAL 半开区间重叠、CAUSAL 同键 + 反向因果),`check_new` 写入时检测 + `scan(scope)` 按需扫描输出 `ContradictionReport`;复用 Layer 0 ConflictType(VALUE/NUMERIC/TEMPORAL→WRITE_WRITE,CAUSAL→CAUSAL_CONTRADICTION),细分维度由 Contradiction.kind 承载
- **实测发现(语义澄清)**:§6.3 触发器对 project_state/task_status/config 覆写时自动把旧值置 valid_to(LWW),故 scan 时状态类「双现行取值冲突」不会出现——状态覆写矛盾由写入时检测(check_new / Layer 4 WRITE_WRITE)落库前拦截,scan 的 VALUE 主要来自枚举谓词;数据集负例 neg_state_overwrite_lww 固化了该语义
- **顺手修复**: `seeds.normalize_name` 正则 `[^a-z0-9]+` → `[^\w]+`(Unicode 感知),修复中文 README 标题(如「## 架构」)生成 entity_id 时抛 ValueError 的潜在崩溃
- middleware 集成(向后兼容新增):`MemoryMiddleware.import_source(source, *, scope=None)` / `inject_seeds(source, *, scope=None)`,scope 缺省回退当前项目;bootstrap 延迟导入避免循环依赖
- `uv run pytest tests/unit/ -q` 510 passed(新增 26:test_importer.py 10 + test_seed_injection.py 8 + test_contradiction.py 8);`uv run ruff check src/ tests/` 通过

**P1-a 进展**(2026-08-21):
- 新增 `src/smilex/memory/concurrency/`:`lock_manager.py`(SHARED/EXCLUSIVE/UPDATE 内存锁,兼容性矩阵仅 SHARED+SHARED 兼容,FIFO 唤醒 + 连续 SHARED 批量放行,超时回退 + acquire_many 锁排序防死锁,UPDATE→EXCLUSIVE 升级,应用层锁非 DB 锁)、`conflict_detection.py`(五类冲突检测: VERSION_STALE/WRITE_WRITE/READ_WRITE 走进程内乐观版本追踪,CAUSAL_CYCLE 沿 predecessor_id 链回溯,CAUSAL_CONTRADICTION/WRITE_WRITE 查现行同键 triple;存储层无 version 列,版本由检测器应用层维护)、`conflict_resolution.py`(§10.3 默认策略表: VERSION_STALE→AUTO_MERGE 字段级三路合并/真冲突升级 MANUAL,WRITE_WRITE/READ_WRITE→AUTO_LAST,CAUSAL_CYCLE→ROLLBACK,CAUSAL_CONTRADICTION→MANUAL)、`controller.py`(ConcurrencyController 三层组合门面 + guard_triple_write)
- middleware 集成(向后兼容可选参数): `MemoryMiddleware.write(..., chain_id=None, detect_conflicts=True)`,relations 涉及实体按 ID 排序加 EXCLUSIVE 锁序列化并发写;冲突自动解决放行,拒绝/人工策略返回 `WriteStatus.CONFLICT + ConflictInfo`;锁超时抛 `LockTimeoutError`;构造函数新增 `concurrency` 注入点
- 偏离说明: 模块文档 05 §4.1 规划文件名 conflict_detector/conflict_resolver 落地为 conflict_detection/conflict_resolution;§4.1 中 chain_coordinator/wait_for_graph/write_queue 未实现(P2,见 §5 演进路径)
- `uv run pytest tests/unit/ -q` 484 passed(新增 50:test_lock_manager.py 19 + test_conflict.py 31);`uv run ruff check src/ tests/` 通过

**P1 退出标准**(全部 ✅,实测证据见主文档 §14.4):
- ✅ 多任务链并发无 lost update(P1-a)
- ✅ Git 历史可批量导入(P1-b,3 提交临时仓库 → 3 时序记忆 + 作者/文件实体,幂等 + 断点续传)
- ✅ 矛盾检测准确率 > 90%(P1-b,27 条标注样本实测 27/27 = 100%)

**P0-b 进展**(2026-08-21):
- 新增 `src/smilex/memory/scheduler/tasks.py`:`CoreTaskRunner`(5 类任务执行函数,全部规则版无 LLM)+ `CoreTaskConfig` + `register_core_tasks(scheduler, storage, *, config)` 装配函数
- 整合: L1 fragments 按「scope + 排序实体集合」分组聚合为 L2(importance 取最大、实体/关系并集、时间取范围),源片段删除;cursor=已扫描 id、state=pending 分组,断点续传不丢分组
- 遗忘: `score = importance × 0.5^(age_days/half_life)`(默认半衰期 30 天),过期(time_end 已过)直接淘汰,低于阈值删除或降权(demote);注:temporal_fragments 无 access_count 列,访问热度由 importance 承载,未改 schema
- 摘要: 超长 fragment 截取 + 句末截断生成「【摘要】」记忆,`fragment_id = 源id + ":summary"` 幂等
- 因果: 链尾 triple 回溯 predecessor_id 建链,upsert `causal_chains`(chain_id="chain:{链尾}" 确定性,support_count=链长,confidence=链上最小)
- 语义: NetworkX 连通分量 → L3 社区缓存(fragment_id 前缀 `semantic:community:`,整批删旧写新,幂等)
- 默认触发: 时间(整合 1h/摘要 30min/语义 2h/遗忘 1d)+ 事件(memory_full/episode_end/session_end/causal_inference)+ 自适应(记忆压力/潜在因果,条件由宿主注入,冷却 5min,对齐 §8.3)
- 复用现有 schema 未改;调度器框架公开 API 未改(仅 `scheduler/__init__.py` 增加导出)
- `uv run pytest tests/unit/ -q` 434 passed(新增 11);`uv run ruff check src/ tests/` 通过

**P0 退出标准**(全部 ✅,实测证据见主文档 §14.3 与 `tests/unit/test_core_tasks.py`):
- ✅ 整合任务自动 L1→L2 流转
- ✅ 长任务可被抢占 + 续传
- ✅ 自适应规则触发冷却正确

**输入**(P1-b):
- 主文档 §10 并发控制层(Layer 4)、§11.2 矛盾检测、§14.4 P1 范围
- 分析文档:`docs/analyse/multi-chain-concurrency-control.md`
- 现有基座:`src/smilex/memory/scheduler/`(调度器 + 5 类核心任务)、`src/smilex/memory/concurrency/`(Layer 4)

**技术债提醒**:D6 遗留 — P1 阶段考虑引入 `predicate_category` 列(EXECUTION_PLAN_GAPS.md §D6 待办)。
