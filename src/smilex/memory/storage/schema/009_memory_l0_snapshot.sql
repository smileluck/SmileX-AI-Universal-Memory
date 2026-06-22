-- memory_l0_snapshot — L0 工作记忆快照(cachebox 持久化)
-- 依据: 主文档 §6.1 字段摘要 + 决策 D4(LRUCache,会话过期由应用层清理)
-- 索引: PRIMARY KEY session_id

CREATE TABLE IF NOT EXISTS memory_l0_snapshot (
    session_id  TEXT PRIMARY KEY,
    data        BLOB NOT NULL,                          -- msgpack 序列化的 L0 缓存
    updated_at  TEXT NOT NULL,
    expires_at  TEXT                                    -- 可选 TTL(决策 D4 不强用)
);

CREATE INDEX IF NOT EXISTS idx_l0_snapshot_updated ON memory_l0_snapshot(updated_at);
CREATE INDEX IF NOT EXISTS idx_l0_snapshot_expires ON memory_l0_snapshot(expires_at) WHERE expires_at IS NOT NULL;
