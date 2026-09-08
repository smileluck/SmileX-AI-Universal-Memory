-- 015: 错误指纹注册表(§ 主动优化收官 — lessonize 教训闭环,DSH 自我纠错模式)
-- fingerprint = sha256(scope|code|规范化message)[:16],scope 折进指纹
-- (同一错误在不同项目分别计数,DSH per-domain 语义);lesson_id 指向
-- 关联的【教训】fragment(同指纹重写指向最新,旧教训失链不删除)。
CREATE TABLE IF NOT EXISTS error_fingerprints (
    fingerprint    TEXT PRIMARY KEY,
    count          INTEGER NOT NULL DEFAULT 1,
    first_seen     TEXT    NOT NULL,
    last_seen      TEXT    NOT NULL,
    sample_code    TEXT    NOT NULL DEFAULT '',
    sample_message TEXT    NOT NULL DEFAULT '',
    lesson_id      TEXT
);

CREATE INDEX IF NOT EXISTS idx_error_fp_last_seen ON error_fingerprints(last_seen);
