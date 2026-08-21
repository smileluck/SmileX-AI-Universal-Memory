-- 011_archive_and_predicate_dict — 历史归档表 + predicate 字典编码(P2 收尾)
-- 依据: 主文档 §11.5/§11.6(冷热分层 + predicate 字典编码)+ 06-layer5 §2.4/§2.5
--
-- 归档表设计(§11.6):
-- - triples_archive / temporal_fragments_archive 与热表同构,额外加 archived_at
-- - 当前状态三元组(valid_to IS NULL)永不归档,故归档表 valid_to 恒非空
-- - 归档记录不保留向量(memory_vectors/vector_links 由 Archiver 同步清理),
--   召回走非向量通道(时间 / 关键词 / scope),见 quality/archiver.py
--
-- predicate 字典编码(§11.5,写入复杂度换体积):
-- - predicate_dict: predicate 字符串 → int code 字典
-- - triples.predicate_code 为冗余编码列(新增,可空);读取路径仍走 TEXT 列,
--   字典编码仅用于: ① 归档表以 code 存储省体积 ② 后续按 code 做统计/压缩
-- - 历史数据在本迁移中一次性回填(INSERT OR IGNORE + UPDATE)

-- ==================== predicate 字典编码 ====================

CREATE TABLE IF NOT EXISTS predicate_dict (
    code      INTEGER PRIMARY KEY,              -- 递增编码
    predicate TEXT    NOT NULL UNIQUE
);

-- triples 增加冗余编码列(读取路径不变,见头注)
ALTER TABLE triples ADD COLUMN predicate_code INTEGER REFERENCES predicate_dict(code);

-- 存量回填: 先建字典,再回填编码
INSERT OR IGNORE INTO predicate_dict(predicate)
SELECT DISTINCT predicate FROM triples;

UPDATE triples
SET predicate_code = (
    SELECT code FROM predicate_dict WHERE predicate_dict.predicate = triples.predicate
)
WHERE predicate_code IS NULL;

CREATE INDEX IF NOT EXISTS idx_triples_predicate_code ON triples(predicate_code)
WHERE predicate_code IS NOT NULL;

-- ==================== 归档表(热表同构 + archived_at) ====================

CREATE TABLE IF NOT EXISTS triples_archive (
    id              TEXT    PRIMARY KEY,
    triple_id       TEXT    NOT NULL,
    subject_id      TEXT    NOT NULL,
    predicate       TEXT    NOT NULL,
    predicate_code  INTEGER REFERENCES predicate_dict(code),
    object_id       TEXT,
    object_value    TEXT,
    scope           TEXT    NOT NULL,
    valid_from      TEXT    NOT NULL,
    valid_to        TEXT,                            -- 归档记录恒非空(当前状态不归档)
    predecessor_id  TEXT,
    causal_level    INTEGER NOT NULL DEFAULT 0,
    confidence      REAL    NOT NULL DEFAULT 1.0,
    certainty       TEXT    NOT NULL DEFAULT 'exact',
    relation_type   TEXT    NOT NULL DEFAULT 'semantic',
    source_closet   TEXT,
    archived_at     TEXT    NOT NULL,                -- 归档时间(ISO 8601)

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
    CHECK (scope = 'global' OR scope LIKE 'project:%' OR scope LIKE 'tenant:%')
);

CREATE TABLE IF NOT EXISTS temporal_fragments_archive (
    id            TEXT    PRIMARY KEY,
    fragment_id   TEXT    NOT NULL,
    time_start    TEXT    NOT NULL,
    time_end      TEXT,
    location_id   TEXT,
    content       TEXT    NOT NULL,
    embedding     TEXT,                              -- 归档时不带向量(见头注)
    entities      TEXT    NOT NULL DEFAULT '[]',
    relations     TEXT    NOT NULL DEFAULT '[]',
    scope         TEXT    NOT NULL DEFAULT 'project',
    layer         TEXT    NOT NULL DEFAULT 'L1',
    importance    REAL    NOT NULL DEFAULT 0.5,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    archived_at   TEXT    NOT NULL,

    CHECK (scope = 'global' OR scope LIKE 'project:%' OR scope LIKE 'tenant:%'),
    CHECK (layer IN ('L0', 'L1', 'L2', 'L3')),
    CHECK (importance >= 0.0 AND importance <= 1.0)
);

CREATE INDEX IF NOT EXISTS idx_triples_archive_scope_time ON triples_archive(scope, valid_from);
CREATE INDEX IF NOT EXISTS idx_triples_archive_valid_to  ON triples_archive(valid_to);
CREATE INDEX IF NOT EXISTS idx_triples_archive_subject   ON triples_archive(subject_id, predicate);
CREATE INDEX IF NOT EXISTS idx_frag_archive_scope_time   ON temporal_fragments_archive(scope, time_start);
CREATE INDEX IF NOT EXISTS idx_frag_archive_updated      ON temporal_fragments_archive(updated_at);
