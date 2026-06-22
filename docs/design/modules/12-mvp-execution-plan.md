# MVP 执行计划(WBS)

> **版本**: v1.0
> **更新日期**: 2026-06-21
> **配套文档**: [agent-memory-design.md §14.2](../agent-memory-design.md) MVP 范围
> **目的**: 把 8 人周 MVP 拆成 35+ 原子任务,锁定依赖、周交付、风险、验收

---

## 0. 状态总览

| 模块 | 人周 | 状态 |
|------|------|------|
| Layer 0 数据模型 | 1 | ✅ 完成(2026-06-21) |
| Layer 1 存储引擎 | 3 | ⏳ 待开始 |
| Layer 2 生命周期 | 2 | ⏳ 待开始 |
| Layer 3 调度+冷启动 | 1 | ⏳ 待开始 |
| MemoryMiddleware | 1 | ⏳ 待开始 |
| **MVP 合计** | **8** | **1/8 完成** |

**剩余预计**: 7 人周(约 35 个工作日,单人工时 7 周)

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
| L1.7 | SQLiteEngine(aiosqlite + WAL pragma + 扩展加载) | 1.5 | L1.6 | ⏳ |
| L1.8 | 查询 — temporal(query_at_time / query_in_range) | 1 | L1.7 | ⏳ |
| L1.9 | 查询 — spatial(query_in_area / query_in_location) | 1 | L1.7 | ⏳ |
| L1.10 | 查询 — graph(find_path / find_n_degree_relations) | 1.5 | L1.7 | ⏳ |
| L1.11 | 查询 — causal(trace_causal_chain) | 1 | L1.7 | ⏳ |
| L1.12 | 查询 — scope filter 应用 | 0.5 | L1.8-L1.11 | ⏳ |
| L1.13 | hybrid_search(RRF fusion) | 1.5 | L1.8-L1.11 | ⏳ |
| L1.14 | StorageEngine 接口封装 | 1 | L1.7-L1.13 | ⏳ |
| L1.15 | 集成测试 + benchmark(P99 < 50ms write) | 1 | L1.14 | ⏳ |

**关键风险**:
- ~~DDL 字段冲突(主文档 §6.1 vs 模块文档 02)→ L1.1 必须先冻结字段~~ ✅ 已解决(见 L1.1 决策)
- sqlite-vec 与 aiosqlite 集成(同步扩展加载 + 异步查询)→ L1.7 可能需要 adapt

---

### 1.3 Layer 2 生命周期(10 人天)

| ID | 任务 | 人天 | 依赖 | 状态 |
|----|------|------|------|------|
| L2.1 | L0WorkingMemory(`LRUCache` per 决策 D4) | 1 | L0,L1.7 | ⏳ |
| L2.2 | L0 快照持久化(memory_l0_snapshot) | 1 | L1.3,L2.1 | ⏳ |
| L2.3 | **决策 D5**: Embedding 方案(维度/库/模型) | 0.5 | — | ⏳ |
| L2.4 | Embedder 实现(sentence-transformers) | 1.5 | L2.3 | ⏳ |
| L2.5 | L1 向量存储(sqlite-vec 适配 + Embedder) | 1.5 | L1.4,L2.4 | ⏳ |
| L2.6 | TokenCounter(tiktoken cl100k_base) | 0.5 | L0 | ⏳ |
| L2.7 | ContextBuilder(Token Budget, §12.1) | 2 | L1.13,L2.5,L2.6 | ⏳ |
| L2.8 | 晋升规则(L0→L1 over 800 tokens) | 1 | L2.1,L2.5 | ⏳ |
| L2.9 | 集成测试(benchmark: recall P99 < 500ms) | 1 | L2.7,L2.8 | ⏳ |

**关键风险**:
- Embedding 维度冲突(§6.4 写 `FLOAT[1536]`,BGE-M3 是 1024 维)→ **L2.3 必须先决策**

---

### 1.4 Layer 3 调度+冷启动(5 人天)

| ID | 任务 | 人天 | 依赖 | 状态 |
|----|------|------|------|------|
| L3.1 | ProjectBootstrap.initialize API | 1 | L1.14,L2.7 | ⏳ |
| L3.2 | Onboarding wizard(5 个核心问题) | 1 | L3.1 | ⏳ |
| L3.3 | 项目模板(web/cli/data/agent YAML) | 1.5 | L3.1 | ⏳ |
| L3.4 | Active learner stub(无 LLM 版本) | 0.5 | L3.1 | ⏳ |
| L3.5 | README 解析 + 实体提取(规则版) | 1 | L3.1 | ⏳ |
| L3.6 | 集成测试(Top-5 召回率 > 60%) | 0.5 | L3.2-L3.5 | ⏳ |

**关键风险**:
- LLM provider 未定 → MVP 用规则提取兜底(见 EXECUTION_PLAN_GAPS.md §3.6)

---

### 1.5 MemoryMiddleware(5 人天)

| ID | 任务 | 人天 | 依赖 | 状态 |
|----|------|------|------|------|
| M.1 | DTO 定义(WriteRequest/Response + RecallRequest/Response + ProjectInit*) | 1 | L0 | ⏳ |
| M.2 | `initialize_project()` 端到端 | 1 | L3.6,M.1 | ⏳ |
| M.3 | `write()` 端到端 | 1 | L1.14,L2.8,M.1 | ⏳ |
| M.4 | `recall()` 端到端 | 1 | L2.7,M.1 | ⏳ |
| M.5 | close_session(L0 清理) | 0.3 | L2.1,M.1 | ⏳ |
| M.6 | 端到端 E2E 测试(创建→写入→检索) | 0.7 | M.2-M.5 | ⏳ |

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
| R2 | Embedding 维度冲突(§6.4 写 1536 vs BGE-M3 1024) | Layer 2 阻塞 | **决策 D5**(L2.3 任务)统一为 1024 维,文档同步修正 | W3 开始前 |
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

### 5.1 pip install smilex-memory 可用 ✋

- [ ] pyproject.toml 完整 metadata(classifiers, license, urls)
- [ ] `uv build` 生成 wheel
- [ ] `pip install dist/smilex_ai_memory-*.whl` 成功
- [ ] 安装后 `python -c "import smilex; print(smilex.__version__)"` 正常
- [ ] README.md 快速开始验证

### 5.2 新项目 < 5 分钟可用 ✋

- [ ] `MemoryMiddleware.initialize_project(name, desc, tech_stack)` 端到端
- [ ] 冷启动流程 6 步全部完成(向导 + 模板 + 主动学习)
- [ ] 实测:在干净的 Python 环境从零到首次 recall < 5 分钟

### 5.3 写入 P99 < 50ms ✋

- [ ] L1.15 benchmark 脚本 `tests/benchmarks/write_latency.py`
- [ ] 1000 次写入采样,P99 < 50ms(单写者,无并发)
- [ ] 若不达标:启用 WAL pragmas + 写入批量化

### 5.4 检索 P99 < 500ms ✋

- [ ] L2.9 benchmark 脚本 `tests/benchmarks/recall_latency.py`
- [ ] 100 次混合检索采样,P99 < 500ms(token budget 4000)
- [ ] 若不达标:检查 sqlite-vec HNSW 参数 / ContextBuilder 算法

### 5.5 冷启动 Top-5 召回率 > 60% ✋

- [ ] L3.6 golden set: 5-10 个典型项目(每个 5-10 个标准 query)
- [ ] 每个标准 query 检查 Top-5 是否包含人工标注的应召回记忆
- [ ] 召回率 = (命中数 / 应召回数) > 60%
- [ ] 不达标时:模板扩充 + 主动学习迭代

---

## 6. 文档同步任务

每完成一个 Layer,需同步更新:

| 触发 | 更新内容 |
|------|---------|
| L1.1 完成 | DDL 冻结后,更新主文档 §6.1 + 模块文档 02 |
| L1.4 完成 | 主文档 §6.4 向量 DDL 与实际一致 |
| L2.3 决策 D5 | 主文档 §6.4 维度 1536 → 1024,§19.1 embedding 行更新 |
| L2.1 完成 | 主文档 §7.2 LRUCache 代码示例(取代 TTLPrefixCache) |
| L2.4 完成 | 主文档 §19.1 增加 sentence-transformers 依赖 |
| M.6 完成 | 主文档 §14.2 退出标准 checklist 标注 ✅ |

---

## 7. 决策记录(本计划新增)

### D5: Embedding 方案(待 L2.3 决策)

**候选**:
- A: sentence-transformers + BGE-M3(本地,1024 维,~2GB 模型)
- B: OpenAI 兼容 API(text-embedding-3-small,1536 维,API 依赖)
- C: ONNX Runtime + BGE-M3 量化版(本地,~500MB)

**待决策时间**: W3 开始前(L2.3 任务)

**推荐**: A — 符合「嵌入式优先」原则(§1.2.3),无 API 成本

---

## 8. 下一会话起点

**最优先**: L1.1 — DDL 融合冻结

**预计工作量**: 2 人天

**输入**:
- 主文档 §6.1 七张核心表字段摘要
- 主文档 §6.3 `project_current_state` 完整 DDL + 触发器
- 模块文档 02-layer1-storage.md 的 DDL 语法参考
- EXECUTION_PLAN_GAPS.md §3.1 字段冲突清单

**输出**:
- `src/smilex/memory/storage/schema/001_entities.sql` ~ `009_*.sql`
- `tests/unit/test_schema.py`(表结构 + 索引 + 触发器可创建)
