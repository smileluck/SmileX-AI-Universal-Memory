-- virtual_tables — sqlite-vec 向量虚拟表 + R-tree 空间虚拟表
-- 依据: 主文档 §6.4 + 决策 D1(sqlite-vec 替代 ChromaDB)
-- 维度: 1024(待决策 D5 最终确认;目前按 BGE-M3 1024 维配置)
-- 注: 这两个虚拟表必须先加载 sqlite-vec 扩展才能创建

-- sqlite-vec HNSW 向量虚拟表(用于 L1 语义检索)
CREATE VIRTUAL TABLE IF NOT EXISTS memory_vectors USING vec0(
    vector_id INTEGER PRIMARY KEY,
    embedding  FLOAT[1024]
);

-- R-tree 空间虚拟表(关联 locations.rowid)
-- id 与 locations.rowid 一致,实现空间过滤
CREATE VIRTUAL TABLE IF NOT EXISTS memory_locations_rtree USING rtree(
    id,                                              -- 对应 locations.rowid
    min_lng, max_lng,                                -- 经度范围
    min_lat, max_lat                                 -- 纬度范围
);
