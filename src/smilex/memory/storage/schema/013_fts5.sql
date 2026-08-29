-- 013_fts5 — BM25 关键词检索通道(FTS5)
-- 依据: docs/analyse/mempalace-optimization-roadmap.md P0「混合检索(RRF)」
--       + LongMemEval 实测漏检(纯向量对精确词如 "animal shelter" 召回不稳)
--
-- 设计:
-- - external content 虚拟表,内容来自 temporal_fragments(content),不双写正文
-- - tokenize='trigram': 中英文通吃(CJK 子串匹配),ASCII 大小写不敏感
-- - 触发器自动同步: 覆盖全部 fragment 写入点(promotion / bulk_importer /
--   tasks / cloner / 归档删除),无需改动任何写入方
-- - 查询侧见 storage/queries/fts.py(bm25_fragment_search)

CREATE VIRTUAL TABLE IF NOT EXISTS fts_fragments USING fts5(
    content,
    content='temporal_fragments',
    content_rowid='rowid',
    tokenize='trigram'
);

-- 同步触发器: INSERT / UPDATE(content) / DELETE
CREATE TRIGGER IF NOT EXISTS trg_fragments_fts_insert
AFTER INSERT ON temporal_fragments BEGIN
    INSERT INTO fts_fragments(rowid, content)
    VALUES (new.rowid, new.content);
END;

CREATE TRIGGER IF NOT EXISTS trg_fragments_fts_update
AFTER UPDATE OF content ON temporal_fragments BEGIN
    INSERT INTO fts_fragments(fts_fragments, rowid, content)
    VALUES ('delete', old.rowid, old.content);
    INSERT INTO fts_fragments(rowid, content)
    VALUES (new.rowid, new.content);
END;

CREATE TRIGGER IF NOT EXISTS trg_fragments_fts_delete
AFTER DELETE ON temporal_fragments BEGIN
    INSERT INTO fts_fragments(fts_fragments, rowid, content)
    VALUES ('delete', old.rowid, old.content);
END;

-- 老库迁移: 存量 fragments 回填 FTS 索引(新库为空表,幂等无害)
INSERT INTO fts_fragments(fts_fragments) VALUES ('rebuild');
