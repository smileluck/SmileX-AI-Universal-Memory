-- temporal_fragments — 时序片段(时间锚定的记忆片段)
-- 依据: 主文档 §6.1 字段摘要
-- 索引: B-tree on time_start + B-tree on time_end

CREATE TABLE IF NOT EXISTS temporal_fragments (
    id            TEXT    PRIMARY KEY,
    fragment_id   TEXT    NOT NULL,                    -- 归一化 ID
    time_start    TEXT    NOT NULL,                    -- ISO 8601
    time_end      TEXT,
    location_id   TEXT,                                -- 关联 locations.id
    content       TEXT    NOT NULL,
    embedding     TEXT,                                -- JSON list of float(应用层序列化)
    entities      TEXT    NOT NULL DEFAULT '[]',        -- JSON array of entity IDs
    relations     TEXT    NOT NULL DEFAULT '[]',        -- JSON array of triple IDs
    scope         TEXT    NOT NULL DEFAULT 'project',
    layer         TEXT    NOT NULL DEFAULT 'L1',        -- L0/L1/L2/L3
    importance    REAL    NOT NULL DEFAULT 0.5,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,

    CHECK (scope = 'global' OR scope LIKE 'project:%' OR scope LIKE 'tenant:%'),
    CHECK (layer IN ('L0', 'L1', 'L2', 'L3')),
    CHECK (importance >= 0.0 AND importance <= 1.0),
    FOREIGN KEY (location_id) REFERENCES locations(id)
);

CREATE INDEX IF NOT EXISTS idx_temporal_start       ON temporal_fragments(time_start);
CREATE INDEX IF NOT EXISTS idx_temporal_end         ON temporal_fragments(time_end) WHERE time_end IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_temporal_scope_layer ON temporal_fragments(scope, layer);
CREATE INDEX IF NOT EXISTS idx_temporal_fragment_id ON temporal_fragments(fragment_id);
