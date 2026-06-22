-- checkpoints — 任务检查点(Layer 3 抢占与恢复用)
-- 依据: 主文档 §6.1 字段摘要 + §9.3 检查点机制
-- 索引: B-tree on task_id

CREATE TABLE IF NOT EXISTS checkpoints (
    id          TEXT    PRIMARY KEY,
    task_id     TEXT    NOT NULL,
    progress    REAL    NOT NULL DEFAULT 0.0,            -- 0.0 ~ 1.0
    step        INTEGER NOT NULL DEFAULT 0,
    state       TEXT    NOT NULL,                        -- JSON: 任务状态序列化
    cursor      BLOB,                                    -- 二进制游标(迭代位置)
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,

    CHECK (progress >= 0.0 AND progress <= 1.0),
    CHECK (step >= 0),
    CHECK (json_valid(state))
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_task ON checkpoints(task_id);
CREATE INDEX IF NOT EXISTS idx_checkpoints_updated ON checkpoints(updated_at);
