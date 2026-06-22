-- causal_chains — 因果链(多个 Triple 串联)
-- 依据: 主文档 §6.1 字段摘要 + Layer 0 CausalChain dataclass
-- 索引: B-tree on chain_id + B-tree on scope
-- 查询时使用 json_each(node_ids) 展开查询(SQLite JSON1)

CREATE TABLE IF NOT EXISTS causal_chains (
    id              TEXT    PRIMARY KEY,
    chain_id        TEXT    NOT NULL UNIQUE,           -- 归一化 ID
    node_ids        TEXT    NOT NULL,                   -- JSON array of triple IDs, 如 ["t1","t2","t3"]
    chain_type      TEXT    NOT NULL DEFAULT 'causal',
    confidence      REAL    NOT NULL DEFAULT 1.0,
    support_count   INTEGER NOT NULL DEFAULT 0,
    scope           TEXT    NOT NULL DEFAULT 'project',
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,

    CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CHECK (support_count >= 0),
    CHECK (scope = 'global' OR scope LIKE 'project:%' OR scope LIKE 'tenant:%'),
    CHECK (json_valid(node_ids)),                       -- 必须 JSON 数组
    CHECK (json_array_length(node_ids) >= 1)
);

CREATE INDEX IF NOT EXISTS idx_causal_chains_chain_id ON causal_chains(chain_id);
CREATE INDEX IF NOT EXISTS idx_causal_chains_scope    ON causal_chains(scope);
CREATE INDEX IF NOT EXISTS idx_causal_chains_type     ON causal_chains(chain_type);
