-- triples — 三元组边 + 因果链前驱
-- 依据: 主文档 §6.1 字段摘要 + §5.2 Layer 0 Triple dataclass + §6.3 触发器语义
-- 索引: B-tree on (subject_id, predicate) + B-tree on predecessor_id
-- 触发器: 见 008_project_current_state.sql
--
-- ⚠️ 设计说明:
-- scope 列存"作用域全路径"(如 "project:abc", "tenant:xyz", "global"),
-- 不是 Layer 0 MemoryScope 枚举的简单字符串(如 "project")。
-- 原因: §6.3 触发器使用 `NEW.scope LIKE 'project:%'` 语义,
-- 需要在同一列中携带作用域类型 + 具体项目/租户标识。
-- Layer 0 ↔ Layer 1 适配由 StorageEngine 负责(enum ↔ 全路径互转)。
--
-- relation_type CHECK 扩展了 §6.3 触发器需要的状态跟踪值:
-- - 原 §5.2 四值: causal/spatial/temporal/semantic(图谱边语义)
-- - §6.3 触发器三值: project_state/task_status/config(状态跟踪)
-- MVP 阶段允许并存,P1 重构时考虑引入独立的 predicate_category 列。

CREATE TABLE IF NOT EXISTS triples (
    id              TEXT    PRIMARY KEY,              -- ULID
    triple_id       TEXT    NOT NULL,                 -- 归一化 ID(用于去重)
    subject_id      TEXT    NOT NULL,
    predicate       TEXT    NOT NULL,
    object_id       TEXT,                             -- 与 object_value 至少一个非空
    object_value    TEXT,
    scope           TEXT    NOT NULL,                 -- 全路径: "project:abc" / "tenant:xyz" / "global"
    valid_from      TEXT    NOT NULL,
    valid_to        TEXT,
    predecessor_id  TEXT,                             -- 因果链前驱
    causal_level    INTEGER NOT NULL DEFAULT 0,
    confidence      REAL    NOT NULL DEFAULT 1.0,
    certainty       TEXT    NOT NULL DEFAULT 'exact', -- exact/high/medium/low/unknown
    relation_type   TEXT    NOT NULL DEFAULT 'semantic',  -- 见上方说明
    source_closet   TEXT,

    CHECK (object_id IS NOT NULL OR object_value IS NOT NULL),
    CHECK (causal_level >= 0),
    CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CHECK (certainty IN ('exact', 'high', 'medium', 'low', 'unknown')),
    CHECK (
        relation_type IN (
            'causal', 'spatial', 'temporal', 'semantic',
            'project_state', 'task_status', 'config'
        )
    ),
    -- scope 必须是 "global" 或 "project:%" 或 "tenant:%" 格式
    CHECK (scope = 'global' OR scope LIKE 'project:%' OR scope LIKE 'tenant:%')
);

CREATE INDEX IF NOT EXISTS idx_triples_subject_pred  ON triples(subject_id, predicate);
CREATE INDEX IF NOT EXISTS idx_triples_object        ON triples(object_id) WHERE object_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_triples_pred          ON triples(predicate);
CREATE INDEX IF NOT EXISTS idx_triples_predecessor   ON triples(predecessor_id) WHERE predecessor_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_triples_scope_time    ON triples(scope, valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_triples_triple_id     ON triples(triple_id);
CREATE INDEX IF NOT EXISTS idx_triples_relation_type ON triples(relation_type);
