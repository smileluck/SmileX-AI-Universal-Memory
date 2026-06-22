-- entities — 实体节点(图谱中的节点)
-- 依据: 主文档 §6.1 字段摘要 + Layer 0 Entity dataclass
-- 索引: B-tree on (scope, entity_id) + B-tree on valid_from

CREATE TABLE IF NOT EXISTS entities (
    id            TEXT    PRIMARY KEY,                -- ULID, 主键
    entity_id     TEXT    NOT NULL,                   -- 归一化 ID(如 "person:alice")
    entity_type   TEXT    NOT NULL,                   -- person/location/object/concept/event
    name          TEXT    NOT NULL,
    scope         TEXT    NOT NULL,                   -- project/tenant/global
    valid_from    TEXT    NOT NULL,                   -- ISO 8601
    valid_to      TEXT,                               -- 可空,表示当前有效
    embedding     TEXT,                               -- JSON list of float(应用层序列化)
    source_closet TEXT,                               -- 双向指针(知识质量层用)

    CHECK (entity_type IN ('person', 'location', 'object', 'concept', 'event')),
    -- scope 全路径: "global" / "project:%" / "tenant:%"
    CHECK (scope = 'global' OR scope LIKE 'project:%' OR scope LIKE 'tenant:%')
);

CREATE INDEX IF NOT EXISTS idx_entities_scope_id   ON entities(scope, entity_id);
CREATE INDEX IF NOT EXISTS idx_entities_valid_from ON entities(valid_from);
CREATE INDEX IF NOT EXISTS idx_entities_type       ON entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_entities_valid_to   ON entities(valid_to) WHERE valid_to IS NOT NULL;
