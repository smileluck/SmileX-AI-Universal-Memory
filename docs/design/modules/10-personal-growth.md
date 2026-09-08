# 个人学习成长模块 — 设计扩展(v2 重写)

> **版本**: v2 · 2026-09-08(原地重写;v1 为 2026-06 设计,建立在一批
> 未实现的抽象[EdgeKind/Provenance/personal: scope]之上,且绕开了双时态
> 三元组、教训闭环、检索反馈等已落地机制 — 评审结论见 §0.2)
> **配套**: [architecture-asbuilt.md](../../architecture-asbuilt.md)(现状权威)
> / [agent-memory-design.md](../agent-memory-design.md)(设计时文档)
> **核心理念**: 项目是短暂的,成长是持续的 — 记忆系统要能跨项目追踪个人能力演进

---

## 0. 问题诊断

### 0.1 项目记忆 vs 个人成长的关键差异

| 维度 | 项目记忆 | 个人学习成长 |
|------|---------|------------|
| **时间性** | 项目周期(周-月) | 跨年甚至终身 |
| **scope** | `project:xxx`(隔离) | 跨项目(贯穿) |
| **数据结构** | 实体 + 三元组(静态) | **等级 + 轨迹**(时序演进) |
| **核心问题** | "这个项目用了什么?" | **"我这半年成长了什么?"** |
| **失败价值** | 项目失败的决策(项目内) | **失败 → 反思 → 经验**(跨项目) |

### 0.2 v1 → v2 的重写依据(评审结论)

v1 的方向正确(与教训闭环/ScopePromoter 思路一致),但机制全部另起炉灶。
v2 的原则:**个人成长数据用现有原语建模,白得全套生命周期机制**:

| v1 设计 | 问题 | v2 替代 |
|---------|------|--------|
| 新增 `personal:` scope 前缀,"无需改枚举" | entities/triples/temporal_fragments 三表都有 `CHECK (scope='global' OR LIKE 'project:%' OR LIKE 'tenant:%')`;真改动面 = 三表迁移 + MemoryScope 枚举 + scope_path + ScopeFilter + build_scope_clause + resolve_scope_str | **不新增 scope**:`skill:` 实体命名空间(同 `tech:`/`class:` 先例),"个人维度"降级为检索时的实体类型过滤 |
| 四张新表(skills/learning_events/reflections/personal_goals) | 不在 fragments 上 → forget/容量治理/删除守卫/dedup/访问反馈全部触不到;等级三处真相源(current_level + level_before/after + level_updated_at) | **零新表**:等级 = 双时态三元组(predecessor 链);事件/复盘 = 约定前缀的 L1 fragment |
| Reflection(复盘)独立体系 | 与 2026-09 落地的错误指纹 + 【教训】闭环平行重复,两种保护语义 | **并入教训闭环**:failure 类复盘回链 error_fingerprint,复用 importance 0.95 保护线 |
| GrowthTracker/LearningPathRecommender/GapIdentifier/ExperienceTransfer 库内算法 | 单用户场景下"库内预计算"过重;`_estimate_hours` 等函数无定义;RL/评分伪精度(1-5 序数做除法) | **算法下沉为 agent 侧推理**:库只保证相关上下文可召回(检索即供给),分析由读取上下文的 agent 即时完成 |
| 写入热路径每事件调 `llm.extract_skills` | 违反"core 零 LLM、Protocol 注入"硬决策,无降级路径 | **SkillExtractor 走 FactExtractor 同构 Protocol**(默认 PassThrough/规则,LLM 可注入) |
| 7 个 MemoryMiddleware 新方法 | 门面膨胀 | **写入复用 memory_write 约定 + 1 个读辅助**;成长曲线查询由 recall 的 entity/time 聚焦参数覆盖(已接线) |
| `encrypt_reflections=True` 按表加密 | SQLCipher 是整库页级加密,按表选择性加密做不到 | 隐私三选一落地(§6),引用已实现的 PIIMasker |

---

## 1. 核心需求拆解(保留自 v1)

五大核心场景:

```
┌──────────────────────────────────────────────────────────────┐
│  场景 1: 学习记录                                            │
│  "我昨天读了《DDD》,学了领域事件的概念"                       │
│  → 记录学习事件 + 关联技能 + 评估掌握度                       │
│                                                              │
│  场景 2: 技能成长追踪                                        │
│  "我的 Python 从入门到精通花了多久?"                         │
│  → 时序追踪等级变化 + 识别突破点/平台期                       │
│                                                              │
│  场景 3: 经验复盘                                            │
│  "上次项目失败是因为没做容量评估,这次要避免"                  │
│  → 结构化复盘 + 跨项目召回相似场景                            │
│                                                              │
│  场景 4: 学习路径推荐                                        │
│  "我想学系统设计,应该先学什么?"                              │
│  → 基于已有技能推荐前置 + 识别盲区                            │
│                                                              │
│  场景 5: 跨项目经验迁移                                      │
│  "项目 A 的缓存设计经验能用到项目 B 吗?"                     │
│  → 技能 ↔ 项目关联 + 相似度匹配                              │
└──────────────────────────────────────────────────────────────┘
```

v2 场景 → 机制映射(全部由现有机制承载,详见 §2-§4):

| 场景 | 写入 | 读取 |
|------|------|------|
| 1 学习记录 | `memory_write` + 【学习】约定 + SkillExtractor | recall(自然语言/实体聚焦) |
| 2 成长追踪 | 双时态 `skill_level` 三元组(supersession 自动闭合旧值) | recall(entity=技能, time=区间) + agent 侧曲线推理 |
| 3 经验复盘 | failure 类走 `memory_report_error` → 教训闭环;其余【复盘】前缀 | 教训随指纹回传;【复盘】经向量/FTS 召回 |
| 4 路径推荐 | `prerequisite` 三元组(共现提升自动沉淀跨项目版) | memory_graph_query(path/neighbors) |
| 5 经验迁移 | ScopePromoter 共现提升(已落地) | recall + graph_query |

---

## 2. 数据模型(零新表,全部现有原语)

### 2.1 技能 = `skill:` 实体命名空间

```python
# 实体(既有 entities 表):
#   entity_id   = "skill:python"          (归一化,跨项目幂等)
#   entity_type = "concept"               (不扩枚举,skill: 前缀即类型信号)
#   name        = "Python"
#   scope       = "global"(跨项目追踪)或 "project:xxx"(项目内技能)
```

- **global 落位**: 技能是跨项目的个人资产,默认写入 global(与 ScopePromoter
  的提升目标一致);项目内试用的新技能可先落 project scope,共现达标后
  由提升任务自动全球化(既有机制,零新增)。
- **检索时"个人维度"过滤** = `entity_id LIKE 'skill:%'`(或 entity_filter
  聚焦 + agent 侧过滤),不引入新 scope、不动 CHECK 约束。
- **Rejected alternative(v1 的 `personal:` 前缀)**: 见 §0.2 表格第一行;
  若未来确需个人 scope 隔离(多用户单库),再做三表 CHECK 迁移 —
  届时 `personal:` 与本设计的实体建模正交,不冲突。

### 2.2 等级/掌握度 = 双时态三元组(predecessor 链)

```python
# 三元组(既有 triples 表,自动获得 supersession 与时间回溯):
#   subject_id    = <skill:python 实体 id>
#   predicate     = "skill_level"
#   object_value  = "3"                     # Dreyfus 1-5 序数
#   relation_type = "config"                # 7 值既有枚举,状态跟踪语义
#   valid_from    = 写入时间
#   predecessor_id = 上一等级三元组 id       # supersession 链
```

- 同键(scope + subject + predicate)写不同值时,**既有 supersession 机制**
  自动闭合旧行(valid_to = 新行 valid_from)+ 新行记 predecessor —
  "等级随时间演进"零成本获得,且 `query_at_time`/`query_in_range` 天然
  支持"我 3 月时是什么水平"。
- **单一真相源**: 等级只存在三元组里;不存在 skills.current_level /
  level_before/after 的冗余(v1 三处真相源问题)。
- 置信度复用 `certainty`(exact=自评确认/high=LLM 推断/…),
  不新增 Provenance 枚举(v1 的抽象地基不存在)。
- `confidence` 表达证据强度(如从 1 次实践推断 vs 10 次)。

### 2.3 学习事件/复盘 = 约定前缀的 L1 fragment

```
【学习】读完《DDD》第 3 章 — 领域事件: 把状态变更建模为不可变事实 …
【复盘】项目 X 失败复盘 — 错误原因: 未做容量评估; 失败条件: …
【教训】…(错误指纹闭环已有协议,见 server/lessons.py)
```

- 写入走 `memory_write`(entities 携带 `skill:` 实体名,relations 携带
  `practiced_in`/`applied_in` 等谓词),立即获得:vec+FTS 双通道检索、
  forget 留存分/访问续命(access_count)、dedup 近重复合并、容量治理、
  审计 — **这是"零新表"的核心收益**。
- 三个前缀语义分层: 【学习】= 输入(读了/练了什么)、【复盘】= 输出
  (结构化反思)、【教训】= 已闭环的失败教训(错误指纹协议管理)。
- **复盘与教训闭环的边界**: 涉及具体错误(有 code/message 可归一)的
  failure 复盘**必须**走 `memory_report_error` → `memory_write(error_fingerprint=…)`
  协议(计数、第 2 次提示、回链、0.95 保护线全自动);【复盘】前缀
  留给无错误指纹的主动复盘(如成功经验、阶段性总结),importance
  建议 ≥0.9(进入删除守卫保护线,与教训同待遇 — v1 未考虑 forget 交互
  的问题在此修复)。

### 2.4 目标 = `goal:` 实体 + 三元组(轻量)

```
entity:  goal:learn-system-design "学会系统设计"
triple:  (goal:learn-system-design, sub_goal, skill:consistent-hashing)
triple:  (skill:consensus, prerequisite, skill:distributed-basics)
```

- v1 的 milestones/blocking_goals/progress 结构化目标管理**砍掉** —
  那是目标管理软件的职责;记忆库只存目标及其关联,进度由 agent 读取
  上下文后自行推断。
- `prerequisite` 边随学习记录共现沉淀,跨项目的通用前置关系由
  ScopePromoter 共现提升自动全球化(场景 4 的数据基础)。

### 2.5 跨项目关联边(既有谓词,无 EdgeKind)

| v1 EdgeKind | v2 谓词(自由文本)+ relation_type |
|-------------|----------------------------------|
| PRACTICED_IN | `practiced_in`(object = 项目实体) |
| LEADS_TO | `prerequisite` |
| APPLIED_IN | `applied_in` |
| LEARNED_FROM | `learned_from`(object = 书/课程实体) |

边属性字典(edge_properties)不需要: 多余属性进 fragment 内容或
certainty/confidence 列。新边类型若需图谱通道过滤,扩 relation_type
CHECK 的既有先例(GAPS D6)仍可用,但本设计不要求。

---

## 3. 检索与上下文供给(算法下沉为 agent 侧推理)

**原则**: 库不做分析,只保证"相关数据召得回"。成长分析(曲线/突破点/
平台期/路径推荐/盲区)是 agent 读到上下文后的即时推理 — LLM 恰好擅长,
而库内预计算伪精度高、不可测(评审对 v1 RL/评分的否决同样适用于此)。

### 3.1 供给通道(全部已接线)

| 分析需求 | 供给方式 |
|---------|---------|
| 成长曲线(等级时间线) | `memory_recall(entity="Python", time_start=…, time_end=…)` — entity 聚焦触发图谱策略,time 触发时序策略;或 `memory_graph_query(causal, triple_id=最新 skill_level)` 沿 predecessor 链取全史 |
| 相似失败复盘 | `memory_report_error` 响应直接携带既有教训(指纹闭环);无指纹的复盘经 recall 向量通道召回 |
| 学习路径 | `memory_graph_query(neighbors, entity="系统设计", relation_types="semantic")` 取前置图 |
| 跨项目经验 | recall(global scope)+ ScopePromoter 已提升的通用模式 |

### 3.2 SkillExtractor(可选 LLM,Protocol 注入)

```python
class SkillExtractor(Protocol):
    """从对话/文档内容提取 (技能名, 证据强度, 建议等级或 None)."""
    async def extract(self, content: str) -> list[SkillEvidence]: ...
```

- 与 FactExtractor 同构: 默认 PassThrough(零 LLM,技能由 agent 显式
  携带在 entities 参数里 — memory_write 的既有约定),LLM 后端可注入,
  失败降级回 PassThrough。
- 提取结果只产生**建议**: 等级三元组的写入仍由 agent 显式发起
  (自评/确认优先,LLM 推断标 certainty=high 以下),不存在隐式改级。

---

## 4. 生命周期与治理交互(v1 缺口,逐项闭合)

| 机制 | 对个人成长数据的行为 |
|------|--------------------|
| forget 留存分 | 【学习】事件正常参与(access_count 续命: 常被召回的技能记录自然留存) |
| 删除守卫(protect_importance ≥0.9) | 【复盘】【教训】建议 importance ≥0.9/0.95 — 高价值反思不参与修剪(教训协议自动 0.95) |
| 容量治理(max_per_scope) | personal 数据在 global/project scope 内正常计数;global 配额需谨慎设置 |
| dedup 近重复合并 | 同一技能的重复学习记录自动合并(既有) |
| 归档(Archiver) | 180/365 天阈值对学习事件偏短 — 可按 scope 调 policy,或接受"老学习记录进归档仍可 include_archived 召回"(默认行为已支持) |
| 跨项目克隆/快照 | skill: 实体随 global 的提升副本走;project 内技能记录随项目快照走(克隆器已支持) |

**保留策略**: 不引入 v1 的 retention_days=3650 第三套规则 — 统一走
forget/Archiver 既有策略,需要更长保留就调 importance,不加新机制。

---

## 5. API(门面零膨胀)

新增**零**个 MemoryMiddleware 方法。全部经既有面:

- 写入: `memory_write(content, entities=["Python"], relations=[…],
  importance=…)`(MCP 工具既有参数;【学习】/【复盘】前缀是内容约定)
- 失败复盘: `memory_report_error(code, message)` → `memory_write(error_fingerprint=…)`(既有协议)
- 读取: `memory_recall(entity=…, time_start=…, time_end=…)` /
  `memory_graph_query(…)`(2026-09 服务化出口已接线)
- 唯一可选新增: `memory_stats(scope="global")` 增加 `skill:` 实体计数
  (面板可见性,一行 SQL,不新增工具)

约定文档化位置: MCP instructions 增加一段"个人成长写入约定"
(何时用【学习】/【复盘】、等级三元组怎么写),与错误循环协议并列。

---

## 6. 隐私(三选一落地,v1 愿望层修正)

个人成长数据(技能水平、失败史、目标)敏感度高于项目事实:

| 方案 | 适用 | 成本 |
|------|------|------|
| **A. 整库 SQLCipher**(已实现,`db_encryption_key`) | 单用户本机库默认推荐 | 零新增(实验性特性已落地) |
| **B. RegexPIIMasker**(已实现,pii_masker 配置) | 担心内容嵌入共享库/导出包时 | 零新增(高置信类别 + 校验位) |
| **C. 明文** | 纯本地开发库 | 0 |

- v1 的"复盘单独加密/独立目录 ~/.smilex/personal/"**删除**: SQLCipher
  是整库页级加密,按表选择性加密不可实现;独立分库与单文件 ADR-001
  及跨 scope 边冲突。
- 导出包: skill 实体/等级三元组随 global 部分是否进包由 CloneFilter
  决定;error_fingerprints 为设备本地数据不导出(克隆器既有决策),
  教训 fragment 本身会随片段走 — 隐私敏感用户选方案 A/B。

---

## 7. 数据量与路线图(重估)

### 7.1 数据量(含向量,v1 漏算一个数量级)

| 项 | 量级/年 | 存储 |
|----|--------|------|
| skill: 实体 | ~50 | 行 + 名字向量(~4KB/条 float32[1024]) |
| 等级/前置三元组 | ~200 | 行(无向量) |
| 【学习】/【复盘】/【教训】片段 | ~600 | 行 + 内容向量 ≈ 2.5MB |
| **合计** | | **≈ 3MB/年**(v1 估 1MB 漏了向量;仍然很小) |

### 7.2 路线图(v1 的"1 周 MVP"在含 CHECK 迁移的现实下不成立;
v2 零迁移,真正的 MVP 是约定层)

- **Phase 1(0.5 天)**: MCP instructions 写入约定 + 【学习】/【复盘】
  前缀文档 + skill_level 三元组约定 + memory_stats 技能计数。
  纯文档/配置,**即刻可用**(agent 按约定写入,检索面已全部接线)。
- **Phase 2(1-2 天,可选)**: SkillExtractor Protocol + 规则后端
  (关键词/书名模式)+ LLM 后端;面板"成长"只读视图(等级时间线,
  读 predecessor 链)。
- **不做**: GrowthTracker/LearningPathRecommender/GapIdentifier/
  ExperienceTransfer 库内实现、个人 scope CHECK 迁移、按表加密、
  结构化目标管理(§0.2 已列依据)。

---

## 8. 与既有机制的协同(保留 v1 结论,引用更新)

- **自动关联**: 技能实体被项目记忆引用(practiced_in/uses)→ 图谱通道
  N 度扩展自然连通"技能 ↔ 项目 ↔ 失败 ↔ 教训"(memory_graph_query 可查)
- **双视角检索**: "项目视角"= recall(默认当前项目 + global);
  "成长视角"= recall(entity=skill:xxx 或 global scope 聚焦) —
  两个视角都是既有 recall 参数组合,无新通道
- **错误指纹闭环**: 失败复盘 → 教训 → 重遇同指纹错误即时回传教训
  (agent 立即纠偏)— 个人成长里最高价值的飞轮已经转起来了

## 9. 结论

v2 把"个人成长"从"一个新子系统"降维成"一组写入约定 + 零新原语":
技能是实体、等级是双时态三元组、复盘并入教训闭环、分析交给 agent。
其代价是放弃了库内预计算的"开箱曲线"(单用户场景下本就伪精度),
换来的是与 forget/dedup/守卫/反馈/提升/克隆/快照全部生命周期机制的
免费互操作 — 这正是 v1 评审的结论:**80% 的场景价值,20% 的机制**。
