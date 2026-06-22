-- vector_links — 向量关联(关系数据 ↔ sqlite-vec rowid 映射)
-- 依据: 主文档 §6.1 字段摘要 + 决策 D1(sqlite-vec 替代 ChromaDB)
-- 用途: sqlite-vec 虚拟表 rowid → 业务实体(entities/triples/temporal_fragments)

CREATE TABLE IF NOT EXISTS vector_links (
    id            TEXT    PRIMARY KEY,
    vector_db     TEXT    NOT NULL DEFAULT 'sqlite_vec', -- 决策 D1: 固定 sqlite_vec
    vector_id     INTEGER NOT NULL,                      -- sqlite-vec memory_vectors.rowid
    entity_id     TEXT,
    triple_id     TEXT,
    fragment_id   TEXT,
    dimension     INTEGER NOT NULL,                      -- 实际维度(待 D5 决策,默认 1024)
    embedding     BLOB,                                  -- 冗余序列化 bytes(可选)
    created_at    TEXT    NOT NULL,

    CHECK (vector_db IN ('sqlite_vec')),
    CHECK (dimension > 0),
    -- 至少有一个业务关联
    CHECK (entity_id IS NOT NULL OR triple_id IS NOT NULL OR fragment_id IS NOT NULL),
    FOREIGN KEY (entity_id)   REFERENCES entities(id),
    FOREIGN KEY (triple_id)   REFERENCES triples(id),
    FOREIGN KEY (fragment_id) REFERENCES temporal_fragments(id)
);

-- 每个 vector_db 内的 vector_id 应唯一(防止重复映射)
CREATE UNIQUE INDEX IF NOT EXISTS uq_vector_links_db_id ON vector_links(vector_db, vector_id);

CREATE INDEX IF NOT EXISTS idx_vector_links_entity   ON vector_links(entity_id)   WHERE entity_id   IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_vector_links_triple   ON vector_links(triple_id)   WHERE triple_id   IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_vector_links_fragment ON vector_links(fragment_id) WHERE fragment_id IS NOT NULL;
