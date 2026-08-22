-- 012_perf_indexes — 性能补索引(H1/M5 配套,纯索引无表结构变更)
-- idx_entities_scope_name: middlewares/memory.py 按名解析实体引用
--   (_resolve_entity_ref / _write_relations 批量解析)与 bulk_importer 的
--   (scope, name) 查找,此前只有 (scope, entity_id) 索引,name 列无索引
-- idx_triples_valid_from: 无 scope 约束的时间点/范围查询(query_at_time 等)
--   此前只能走 (scope, valid_from, valid_to) 复合索引或全表扫描

CREATE INDEX IF NOT EXISTS idx_entities_scope_name ON entities(scope, name);
CREATE INDEX IF NOT EXISTS idx_triples_valid_from  ON triples(valid_from);
