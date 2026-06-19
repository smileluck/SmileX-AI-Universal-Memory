# PostgreSQL 时空图谱因果一体化设计方案

> 设计日期：2026-05-25
> 基于：MemPalace Knowledge Graph + 模糊记忆系统

---

## 1. 整体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                    Hybrid Memory System                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐     │
│  │   Vector DB  │◄──►│  PostgreSQL  │◄──►│  Working     │     │
│  │  (ChromaDB)  │    │  (时空调仓)   │    │  Memory      │     │
│  └──────────────┘    └──────────────┘    └──────────────┘     │
│         │                   │                   │               │
│         │                   │                   │               │
│         ▼                   ▼                   ▼               │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │              PostgreSQL 时空图谱因果引擎                 │    │
│  │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐      │    │
│  │  │时序索引  │ │空间索引  │ │图谱索引  │ │因果索引  │      │    │
│  │  │temporal │ │spatial  │ │recursive│ │causal   │      │    │
│  │  │ GIST    │ │ PostGIS │ │  CTE    │ │ chain  │      │    │
│  │  └─────────┘ └─────────┘ └─────────┘ └─────────┘      │    │
│  └─────────────────────────────────────────────────────────┘    │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 2. Schema 设计

### 2.1 核心表结构

```sql
-- ============================================
-- 表1：实体表 (Entities)
-- 存储记忆中的实体（人物、地点、概念、物体）
-- ============================================
CREATE TABLE entities (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id       VARCHAR(255) NOT NULL,        -- 归一化 ID
    entity_type     VARCHAR(50) NOT NULL,          -- person, location, object, concept, event
    name            TEXT NOT NULL,                 -- 原始名称
    name_normalized VARCHAR(255),                  -- 归一化名称
    
    -- 时空属性
    valid_from      TIMESTAMPTZ NOT NULL,
    valid_to        TIMESTAMPTZ,
    time_certainty  VARCHAR(20) DEFAULT 'exact',   -- exact, approx, range, unknown
    
    -- 空间属性（PostGIS）
    location        GEOMETRY(POINT, 4326),         -- 精确坐标
    location_fuzzy  JSONB,                         -- 模糊位置 {"room": "客厅", "floor": 1}
    location_id     VARCHAR(255),                   -- 关联 locations 表
    
    -- 语义向量（用于混合检索）
    embedding       vector(1536),
    
    -- 元数据
    metadata        JSONB DEFAULT '{}',
    source_type     VARCHAR(50),                   -- palace, user_input, sensor
    source_id       UUID,                         -- 关联源记录
    
    -- 审计字段
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    
    -- 约束
    CONSTRAINT valid_time_range CHECK (valid_to IS NULL OR valid_to > valid_from)
);

-- 索引
CREATE INDEX idx_entities_id_normalized ON entities(entity_id);
CREATE INDEX idx_entities_type ON entities(entity_type);
CREATE INDEX idx_entities_location ON entities USING GIST(location);
CREATE INDEX idx_entities_valid_time ON entities USING GIST(tstzrange(valid_from, valid_to));
CREATE INDEX idx_entities_embedding ON entities USING ivfflat(embedding vector_cosine_ops);

-- ============================================
-- 表2：三元组表 (Triples)
-- 存储实体间关系 + 时空图谱 + 因果链
-- ============================================
CREATE TABLE triples (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    triple_id       VARCHAR(255) NOT NULL,
    
    -- 三元组核心
    subject_id      UUID NOT NULL REFERENCES entities(id),
    predicate       VARCHAR(100) NOT NULL,         -- 关系类型: 位于, 认识, 导致, 包含...
    object_id       UUID,                          -- 值为实体 ID（可为 NULL，用于属性）
    object_value    TEXT,                           -- 或直接值
    
    -- 时空属性
    valid_from      TIMESTAMPTZ NOT NULL,
    valid_to        TIMESTAMPTZ,
    time_certainty  VARCHAR(20) DEFAULT 'exact',
    
    -- 因果链
    predecessor_id  UUID REFERENCES triples(id),   -- 前因
    successor_ids    UUID[] DEFAULT '{}',           -- 后果（冗余，加速查询）
    causal_level     INTEGER DEFAULT 0,             -- 因果层级（0=直接原因, 1=原因的原因）
    
    -- 置信度（模糊推理）
    confidence       DECIMAL(3,2) DEFAULT 1.0,     -- 0.00-1.00
    certainty        VARCHAR(20) DEFAULT 'exact',  -- exact, high, medium, low, unknown
    
    -- 图谱属性
    edge_weight      DECIMAL(5,2) DEFAULT 1.0,      -- 边权重（用于路径计算）
    relation_type    VARCHAR(50),                   -- causal, spatial, temporal, semantic
    
    -- 元数据
    metadata         JSONB DEFAULT '{}',
    source_id        UUID,
    
    -- 审计字段
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW(),
    
    CONSTRAINT valid_time_range CHECK (valid_to IS NULL OR valid_to > valid_from)
);

-- 索引
CREATE INDEX idx_triples_subject ON triples(subject_id);
CREATE INDEX idx_triples_object ON triples(object_id);
CREATE INDEX idx_triples_predicate ON triples(predicate);
CREATE INDEX idx_triples_predecessor ON triples(predecessor_id);
CREATE INDEX idx_triples_valid_time ON triples USING GIST(tstzrange(valid_from, valid_to));
CREATE INDEX idx_triples_causal_level ON triples(causal_level);

-- ============================================
-- 表3：位置表 (Locations)
-- 支持抽象位置（房间、区域）和精确坐标
-- ============================================
CREATE TABLE locations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    location_id     VARCHAR(255) NOT NULL UNIQUE,
    
    -- 位置层级（抽象）
    name            TEXT NOT NULL,                 -- "客厅"、"厨房"
    location_type   VARCHAR(50),                   -- room, building, city, region, coordinate
    parent_id       UUID REFERENCES locations(id), -- 父级位置
    path            LTREE,                         -- 层级路径: "home.living_room"
    
    -- 精确坐标
    coordinates     GEOMETRY(GEOMETRY, 4326),
    coordinates_fuzzy JSONB,                       -- {"center": [x,y], "radius": 5}
    
    -- 关联实体（谁在这个位置）
    entities        JSONB DEFAULT '[]',            -- [{entity_id, valid_from, valid_to}]
    
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 索引
CREATE INDEX idx_locations_path ON locations USING GIST(path);
CREATE INDEX idx_locations_coordinates ON locations USING GIST(coordinates);

-- ============================================
-- 表4：时序片段表 (Temporal Fragments)
-- 存储时间线上的记忆片段
-- ============================================
CREATE TABLE temporal_fragments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fragment_id     VARCHAR(255) NOT NULL UNIQUE,
    
    -- 时间（模糊）
    time_start      TIMESTAMPTZ NOT NULL,
    time_end        TIMESTAMPTZ,
    time_range      TSTZRANGE,                     -- PostgreSQL 范围类型
    
    -- 空间
    location_id     UUID REFERENCES locations(id),
    location_fuzzy  JSONB,
    
    -- 片段内容（向量）
    content         TEXT,                          -- 原始描述
    embedding       vector(1536),                  -- 向量表示
    
    -- 关联实体
    entities        UUID[] DEFAULT '{}',
    related_triples UUID[] DEFAULT '{}',
    
    -- 片段属性
    fragment_type   VARCHAR(50),                   -- observation, event, action, thought
    importance      INTEGER DEFAULT 5,             -- 1-10 重要性评分
    
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 索引
CREATE INDEX idx_fragments_time ON temporal_fragments USING GIST(time_range);
CREATE INDEX idx_fragments_location ON temporal_fragments(location_id);
CREATE INDEX idx_fragments_embedding ON temporal_fragments USING ivfflat(embedding vector_cosine_ops);

-- ============================================
-- 表5：因果链表 (Causal Chains)
-- 显式存储因果推理结果
-- ============================================
CREATE TABLE causal_chains (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chain_id        VARCHAR(255) NOT NULL UNIQUE,
    
    -- 链信息
    chain_name      TEXT,
    description     TEXT,
    
    -- 链节点（有序）
    node_ids        UUID[] NOT NULL,               -- 按因果顺序排列
    chain_length    INTEGER,
    
    -- 链属性
    chain_type      VARCHAR(50),                   -- temporal, spatial, logical, learned
    confidence      DECIMAL(3,2) DEFAULT 1.0,
    
    -- 统计
    support_count   INTEGER DEFAULT 1,             -- 支持该因果链的记忆数量
    last_verified   TIMESTAMPTZ,
    
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 索引
CREATE INDEX idx_chains_nodes ON causal_chains USING GIN(node_ids);
CREATE INDEX idx_chains_confidence ON causal_chains(confidence);

-- ============================================
-- 表6：向量关联表 (Vector Links)
-- 连接向量数据库与 PostgreSQL
-- ============================================
CREATE TABLE vector_links (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- 向量数据库端
    vector_db       VARCHAR(50) NOT NULL,          -- chromadb, qdrant, weaviate
    collection_name VARCHAR(255),
    vector_id       VARCHAR(255),
    embedding       vector(1536),
    
    -- PostgreSQL 端
    entity_id       UUID REFERENCES entities(id),
    triple_id       UUID REFERENCES triples(id),
    fragment_id     UUID REFERENCES temporal_fragments(id),
    
    -- 关联元数据
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    
    CONSTRAINT one_target CHECK (
        (entity_id IS NOT NULL)::int +
        (triple_id IS NOT NULL)::int +
        (fragment_id IS NOT NULL)::int = 1
    )
);

-- 索引
CREATE INDEX idx_vector_links_entity ON vector_links(entity_id);
CREATE INDEX idx_vector_links_vector ON vector_links USING ivfflat(embedding vector_cosine_ops);

-- ============================================
-- 触发器：自动更新 updated_at
-- ============================================
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER entities_updated_at BEFORE UPDATE ON entities
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER triples_updated_at BEFORE UPDATE ON triples
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER causal_chains_updated_at BEFORE UPDATE ON causal_chains
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
```

---

## 3. 时序检索实现

### 3.1 时间范围查询

```sql
-- ============================================
-- 查询某个时间点的记忆状态
-- ============================================
CREATE OR REPLACE FUNCTION query_at_time(
    query_time TIMESTAMPTZ,
    entity_filter VARCHAR(50) DEFAULT NULL
)
RETURNS TABLE (
    entity_id VARCHAR,
    name TEXT,
    entity_type VARCHAR,
    valid_from TIMESTAMPTZ,
    valid_to TIMESTAMPTZ
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        e.entity_id,
        e.name,
        e.entity_type,
        e.valid_from,
        e.valid_to
    FROM entities e
    WHERE e.valid_from <= query_time
      AND (e.valid_to IS NULL OR e.valid_to > query_time)
      AND (entity_filter IS NULL OR e.entity_type = entity_filter)
    ORDER BY e.entity_type, e.name;
END;
$$ LANGUAGE plpgsql;

-- ============================================
-- 查询时间段内的记忆
-- ============================================
CREATE OR REPLACE FUNCTION query_in_range(
    start_time TIMESTAMPTZ,
    end_time TIMESTAMPTZ
)
RETURNS TABLE (
    entity_id VARCHAR,
    name TEXT,
    entity_type VARCHAR,
    overlap_range TSTZRANGE
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        e.entity_id,
        e.name,
        e.entity_type,
        tstzrange(
            GREATEST(e.valid_from, start_time),
            LEAST(e.valid_to, end_time)
        ) AS overlap_range
    FROM entities e
    WHERE e.valid_from <= end_time
      AND (e.valid_to IS NULL OR e.valid_to >= start_time)
    ORDER BY e.valid_from;
END;
$$ LANGUAGE plpgsql;
```

### 3.2 时间线构建

```sql
-- ============================================
-- 构建实体的完整时间线
-- ============================================
CREATE OR REPLACE FUNCTION build_entity_timeline(
    p_entity_id VARCHAR
)
RETURNS TABLE (
    event_time TIMESTAMPTZ,
    event_type VARCHAR,
    description TEXT,
    related_entities JSONB
) AS $$
BEGIN
    RETURN QUERY
    -- 实体创建/更新
    SELECT 
        e.created_at,
        'entity_created'::VARCHAR,
        '实体创建: ' || e.name,
        jsonb_build_object('entity', e.entity_id)
    FROM entities e
    WHERE e.entity_id = p_entity_id
    
    UNION ALL
    
    -- 关系变化
    SELECT 
        t.created_at,
        'relationship',
        t.subject_id::TEXT || ' ' || t.predicate || ' ' || COALESCE(t.object_id::TEXT, t.object_value),
        jsonb_build_object(
            'subject', t.subject_id,
            'predicate', t.predicate,
            'object', t.object_id
        )
    FROM triples t
    JOIN entities e ON t.subject_id = e.id
    WHERE e.entity_id = p_entity_id
    
    UNION ALL
    
    -- 位置变化
    SELECT 
        l.updated_at,
        'location_change',
        '位置: ' || l.name,
        jsonb_build_object('location', l.location_id)
    FROM locations l
    WHERE l.id IN (
        SELECT unnest(entities->0->'entity_id')::UUID
        FROM locations
        WHERE entities @> jsonb_build_array(jsonb_build_object('entity_id', p_entity_id))
    )
    
    ORDER BY event_time;
END;
$$ LANGUAGE plpgsql;
```

---

## 4. 空间检索实现

### 4.1 位置查询

```sql
-- ============================================
-- 查询范围内的实体
-- ============================================
CREATE OR REPLACE FUNCTION query_in_area(
    center_lng DECIMAL,
    center_lat DECIMAL,
    radius_meters INTEGER DEFAULT 100
)
RETURNS TABLE (
    entity_id VARCHAR,
    entity_name TEXT,
    entity_type VARCHAR,
    distance_meters DOUBLE PRECISION
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        e.entity_id,
        e.name,
        e.entity_type,
        ST_Distance(
            e.location::geography,
            ST_SetSRID(ST_MakePoint(center_lng, center_lat), 4326)::geography
        ) AS distance_meters
    FROM entities e
    WHERE e.location IS NOT NULL
      AND ST_DWithin(
          e.location::geography,
          ST_SetSRID(ST_MakePoint(center_lng, center_lat), 4326)::geography,
          radius_meters
      )
    ORDER BY distance_meters;
END;
$$ LANGUAGE plpgsql;

-- ============================================
-- 查询抽象位置内的实体
-- ============================================
CREATE OR REPLACE FUNCTION query_in_location(
    location_path LTREE,
    include_children BOOLEAN DEFAULT TRUE
)
RETURNS TABLE (
    entity_id VARCHAR,
    entity_name TEXT,
    location_name TEXT
) AS $$
BEGIN
    IF include_children THEN
        RETURN QUERY
        SELECT 
            e.entity_id,
            e.name,
            l.name
        FROM entities e
        JOIN locations l ON e.location_id = l.id
        WHERE l.path <@ location_path
           OR l.path = location_path;
    ELSE
        RETURN QUERY
        SELECT 
            e.entity_id,
            e.name,
            l.name
        FROM entities e
        JOIN locations l ON e.location_id = l.id
        WHERE l.path = location_path;
    END IF;
END;
$$ LANGUAGE plpgsql;

-- ============================================
-- 位置层级查询
-- ============================================
CREATE OR REPLACE FUNCTION get_location_hierarchy(
    location_id UUID
)
RETURNS TABLE (
    level INTEGER,
    location_id UUID,
    location_name TEXT,
    path LTREE
) AS $$
BEGIN
    RETURN QUERY
    WITH RECURSIVE hierarchy AS (
        -- 起始节点
        SELECT 
            0 AS level,
            l.id,
            l.name,
            l.path
        FROM locations l
        WHERE l.id = location_id
        
        UNION ALL
        
        -- 向上追溯父节点
        SELECT 
            h.level + 1,
            p.id,
            p.name,
            p.path
        FROM locations p
        JOIN hierarchy h ON h.parent_id = p.id
    )
    SELECT * FROM hierarchy ORDER BY level;
END;
$$ LANGUAGE plpgsql;
```

### 4.2 空间关系推理

```sql
-- ============================================
-- 推理实体间空间关系
-- ============================================
CREATE OR REPLACE FUNCTION infer_spatial_relations(
    entity_a_id UUID,
    entity_b_id UUID
)
RETURNS TABLE (
    relation VARCHAR(50),
    confidence DECIMAL(3,2)
) AS $$
DECLARE
    loc_a GEOMETRY;
    loc_b GEOMETRY;
    dist DOUBLE PRECISION;
BEGIN
    -- 获取位置
    SELECT location INTO loc_a FROM entities WHERE id = entity_a_id;
    SELECT location INTO loc_b FROM entities WHERE id = entity_b_id;
    
    IF loc_a IS NULL OR loc_b IS NULL THEN
        RETURN;
    END IF;
    
    -- 计算距离
    dist := ST_Distance(loc_a::geography, loc_b::geography);
    
    -- 根据距离判断关系
    IF dist < 5 THEN
        RETURN QUERY SELECT '相邻'::VARCHAR, 0.95::DECIMAL(3,2);
    ELSIF dist < 50 THEN
        RETURN QUERY SELECT '同一区域'::VARCHAR, 0.85::DECIMAL(3,2);
    ELSIF dist < 500 THEN
        RETURN QUERY SELECT '附近'::VARCHAR, 0.70::DECIMAL(3,2);
    ELSE
        RETURN QUERY SELECT '距离较远'::VARCHAR, 0.50::DECIMAL(3,2);
    END IF;
END;
$$ LANGUAGE plpgsql;
```

---

## 5. 图谱检索实现（递归 CTE）

### 5.1 路径查询

```sql
-- ============================================
-- 查找两实体间的最短路径
-- ============================================
CREATE OR REPLACE FUNCTION find_path(
    start_entity_id VARCHAR,
    end_entity_id VARCHAR,
    max_depth INTEGER DEFAULT 5
)
RETURNS TABLE (
    path UUID[],
    total_weight DECIMAL,
    path_length INTEGER
) AS $$
BEGIN
    RETURN QUERY
    WITH RECURSIVE path_search AS (
        -- 起始节点
        SELECT 
            ARRAY[e_start.id] AS path,
            e_start.id AS current_id,
            0::DECIMAL AS total_weight,
            1 AS depth
        FROM entities e_start
        WHERE e_start.entity_id = start_entity_id
        
        UNION ALL
        
        -- 递归查找
        SELECT 
            ps.path || t.object_id,
            t.object_id,
            ps.total_weight + t.edge_weight,
            ps.depth + 1
        FROM path_search ps
        JOIN triples t ON t.subject_id = ps.current_id
        WHERE ps.depth < max_depth
          AND t.object_id != ALL(ps.path)  -- 避免环
          AND NOT EXISTS (
              SELECT 1 FROM unnest(ps.path) AS p WHERE p = t.object_id
          )
    )
    SELECT 
        ps.path,
        ps.total_weight,
        ps.depth
    FROM path_search ps
    JOIN entities e_end ON e_end.id = ps.current_id
    WHERE e_end.entity_id = end_entity_id
    ORDER BY ps.total_weight
    LIMIT 1;
END;
$$ LANGUAGE plpgsql;

-- ============================================
-- 查找N度关系
-- ============================================
CREATE OR REPLACE FUNCTION find_n_degree_relations(
    entity_id VARCHAR,
    degree INTEGER DEFAULT 1
)
RETURNS TABLE (
    related_entity_id VARCHAR,
    relationship_path JSONB,
    degree_level INTEGER
) AS $$
BEGIN
    RETURN QUERY
    WITH RECURSIVE relations AS (
        -- 1度: 直接关系
        SELECT 
            e_rel.entity_id,
            jsonb_build_array(
                jsonb_build_object(
                    'from', e_src.entity_id,
                    'predicate', t.predicate,
                    'to', e_rel.entity_id
                )
            ) AS path,
            1 AS degree
        FROM triples t
        JOIN entities e_src ON t.subject_id = e_src.id
        LEFT JOIN entities e_rel ON t.object_id = e_rel.id
        WHERE e_src.entity_id = entity_id
        
        UNION ALL
        
        -- N度: 递归扩展
        SELECT 
            e_rel.entity_id,
            r.relationship_path || jsonb_build_object(
                'from', e_src.entity_id,
                'predicate', t.predicate,
                'to', e_rel.entity_id
            ),
            r.degree_level + 1
        FROM relations r
        JOIN triples t ON t.subject_id IN (
            SELECT unnest(path_to_ids(r.relationship_path))
        )
        JOIN entities e_src ON t.subject_id = e_src.id
        LEFT JOIN entities e_rel ON t.object_id = e_rel.id
        WHERE r.degree_level < degree
          AND NOT e_rel.entity_id = ANY(path_entity_ids(r.relationship_path))
    )
    SELECT DISTINCT ON (related_entity_id) 
        r.related_entity_id,
        r.relationship_path,
        r.degree_level
    FROM relations r
    ORDER BY r.related_entity_id, r.degree_level;
END;
$$ LANGUAGE plpgsql;
```

### 5.2 社区发现

```sql
-- ============================================
-- 基于关系强度发现实体社区
-- ============================================
CREATE OR REPLACE FUNCTION discover_communities(
    min_connection_count INTEGER DEFAULT 3
)
RETURNS TABLE (
    community_id INTEGER,
    entity_id VARCHAR,
    entity_type VARCHAR,
    connection_count INTEGER
) AS $$
BEGIN
    RETURN QUERY
    WITH entity_connections AS (
        SELECT 
            e.entity_id,
            e.entity_type,
            COUNT(DISTINCT t.object_id) AS connection_count
        FROM entities e
        LEFT JOIN triples t ON t.subject_id = e.id
        GROUP BY e.entity_id, e.entity_type
        HAVING COUNT(DISTINCT t.object_id) >= min_connection_count
    ),
    community_assignment AS (
        SELECT 
            ec.*,
            DENSE_RANK() OVER (ORDER BY ec.connection_count DESC) AS community_id
        FROM entity_connections ec
    )
    SELECT * FROM community_assignment ORDER BY community_id, connection_count DESC;
END;
$$ LANGUAGE plpgsql;
```

---

## 6. 因果追溯实现

### 6.1 因果链查询

```sql
-- ============================================
-- 追溯事件的原因链
-- ============================================
CREATE OR REPLACE FUNCTION trace_causal_chain(
    event_id UUID,
    direction VARCHAR(10) DEFAULT 'backward',  -- backward=原因, forward=结果
    max_depth INTEGER DEFAULT 10
)
RETURNS TABLE (
    chain_node JSONB,
    depth INTEGER,
    accumulated_confidence DECIMAL
) AS $$
DECLARE
    current_id UUID;
    current_confidence DECIMAL := 1.0;
BEGIN
    -- 获取起始节点
    IF direction = 'backward' THEN
        SELECT predecessor_id INTO current_id FROM triples WHERE id = event_id;
    ELSE
        -- forward: 获取第一个后继
        SELECT successor_ids[1] INTO current_id FROM triples WHERE id = event_id;
    END IF;
    
    -- 递归追溯
    WHILE current_id IS NOT NULL AND depth < max_depth LOOP
        RETURN QUERY
        SELECT 
            jsonb_build_object(
                'triple_id', t.id,
                'subject', e_subj.name,
                'predicate', t.predicate,
                'object', e_obj.name,
                'confidence', t.confidence,
                'time', t.valid_from
            ),
            depth,
            current_confidence * t.confidence
        FROM triples t
        JOIN entities e_subj ON t.subject_id = e_subj.id
        LEFT JOIN entities e_obj ON t.object_id = e_obj.id
        WHERE t.id = current_id;
        
        current_confidence := current_confidence * (SELECT confidence FROM triples WHERE id = current_id);
        
        -- 获取下一个节点
        IF direction = 'backward' THEN
            SELECT predecessor_id INTO current_id FROM triples WHERE id = current_id;
        ELSE
            SELECT successor_ids[1] INTO current_id FROM triples WHERE id = current_id;
        END IF;
        
        depth := depth + 1;
    END LOOP;
END;
$$ LANGUAGE plpgsql;

-- ============================================
-- 推理因果关系（基于时序和共现）
-- ============================================
CREATE OR REPLACE FUNCTION infer_causal_relations(
    entity_id VARCHAR,
    time_window INTERVAL DEFAULT '1 hour'
)
RETURNS TABLE (
    cause_entity VARCHAR,
    effect_entity VARCHAR,
    predicate VARCHAR,
    confidence DECIMAL,
    inference_type VARCHAR
) AS $$
BEGIN
    RETURN QUERY
    -- 时序因果推理: A 在 B 之前发生，且常一起出现
    SELECT 
        e_cause.entity_id,
        e_effect.entity_id,
        'caused_by'::VARCHAR,
        COUNT(*)::DECIMAL / 
            GREATEST(
                (SELECT COUNT(*) FROM triples WHERE object_id IN (
                    SELECT id FROM entities WHERE entity_id = e_cause.entity_id
                )),
                1
            ) AS confidence,
        'temporal_cooccurrence'::VARCHAR
    FROM triples t1
    JOIN entities e_cause ON t1.object_id = e_cause.id
    JOIN triples t2 ON t2.subject_id = t1.subject_id
        AND t2.predicate = 'precedes'
    JOIN entities e_effect ON t2.object_id = e_effect.id
    WHERE t1.valid_from BETWEEN t2.valid_from AND t2.valid_from + time_window
      AND e_cause.entity_id != e_effect.entity_id
    GROUP BY e_cause.entity_id, e_effect.entity_id
    ORDER BY confidence DESC
    LIMIT 20;
END;
$$ LANGUAGE plpgsql;
```

### 6.2 因果链构建

```sql
-- ============================================
-- 从片段构建因果链
-- ============================================
CREATE OR REPLACE FUNCTION build_causal_chain(
    fragment_ids UUID[]
)
RETURNS UUID AS $$
DECLARE
    new_chain_id UUID;
    chain_nodes UUID[] := '{}';
    prev_node UUID;
BEGIN
    -- 按时间排序片段
    FOR node_id IN
        SELECT f.id
        FROM unnest(fragment_ids) WITH ORDINALITY AS f(id, ord)
        JOIN temporal_fragments f ON f.id = f.id
        ORDER BY f.time_start
    LOOP
        chain_nodes := array_append(chain_nodes, node_id);
        
        -- 创建三元组连接
        IF prev_node IS NOT NULL THEN
            INSERT INTO triples (
                subject_id, predicate, object_id,
                predecessor_id, causal_level, relation_type
            ) VALUES (
                prev_node, 'leads_to', node_id,
                prev_node, 1, 'causal'
            ) RETURNING id INTO prev_node;
        ELSE
            prev_node := node_id;
        END IF;
    END LOOP;
    
    -- 创建因果链记录
    INSERT INTO causal_chains (
        chain_id, chain_name, node_ids, chain_length, chain_type
    ) VALUES (
        'chain_' || gen_random_uuid()::TEXT,
        'Auto-built chain',
        chain_nodes,
        array_length(chain_nodes, 1),
        'temporal'
    ) RETURNING id INTO new_chain_id;
    
    RETURN new_chain_id;
END;
$$ LANGUAGE plpgsql;
```

---

## 7. 混合检索策略

### 7.1 综合查询函数

```sql
-- ============================================
-- 混合检索：时 + 空 + 语义 + 图谱
-- ============================================
CREATE OR REPLACE FUNCTION hybrid_memory_search(
    -- 语义条件
    query_text TEXT,
    query_embedding vector,
    
    -- 时序条件
    time_start TIMESTAMPTZ DEFAULT NULL,
    time_end TIMESTAMPTZ DEFAULT NULL,
    time_fuzzy BOOLEAN DEFAULT FALSE,
    
    -- 空间条件
    location_lng DECIMAL DEFAULT NULL,
    location_lat DECIMAL DEFAULT NULL,
    location_radius INTEGER DEFAULT 100,
    location_path LTREE DEFAULT NULL,
    
    -- 图谱条件
    related_entity VARCHAR DEFAULT NULL,
    relationship_type VARCHAR DEFAULT NULL,
    
    -- 因果条件
    causal_depth INTEGER DEFAULT 0,
    include_causal_chain BOOLEAN DEFAULT FALSE,
    
    -- 模糊置信度
    min_confidence DECIMAL DEFAULT 0.5,
    
    -- 分页
    limit_count INTEGER DEFAULT 20,
    offset_start INTEGER DEFAULT 0
)
RETURNS TABLE (
    result_type VARCHAR,        -- entity, triple, fragment
    result_id UUID,
    result_data JSONB,
    relevance_score DECIMAL,
    matched_conditions JSONB
) AS $$
BEGIN
    RETURN QUERY
    WITH semantic_matches AS (
        -- 语义相似度匹配
        SELECT 
            'entity'::VARCHAR AS result_type,
            vl.entity_id AS result_id,
            e.embedding <=> query_embedding AS distance,
            ARRAY['semantic'] AS matched_conditions
        FROM vector_links vl
        JOIN entities e ON vl.entity_id = e.id
        WHERE query_embedding IS NOT NULL
          AND vl.entity_id IS NOT NULL
    ),
    temporal_matches AS (
        -- 时序匹配
        SELECT 
            'entity'::VARCHAR AS result_type,
            e.id AS result_id,
            0.0 AS distance,
            ARRAY['temporal'] AS matched_conditions
        FROM entities e
        WHERE (time_start IS NULL OR e.valid_from <= time_end)
          AND (time_end IS NULL OR e.valid_to >= time_start OR e.valid_to IS NULL)
    ),
    spatial_matches AS (
        -- 空间匹配
        SELECT 
            'entity'::VARCHAR AS result_type,
            e.id AS result_id,
            0.0 AS distance,
            ARRAY['spatial'] AS matched_conditions
        FROM entities e
        WHERE 
            (location_lng IS NULL OR 
             ST_DWithin(
                 e.location::geography,
                 ST_SetSRID(ST_MakePoint(location_lng, location_lat), 4326)::geography,
                 location_radius
             ))
            OR (location_path IS NOT NULL AND e.location_id IN (
                SELECT id FROM locations WHERE path <@ location_path
            ))
    ),
    graph_matches AS (
        -- 图谱匹配
        SELECT 
            'triple'::VARCHAR AS result_type,
            t.id AS result_id,
            0.0 AS distance,
            ARRAY['graph'] AS matched_conditions
        FROM triples t
        WHERE related_entity IS NOT NULL
          AND (
              t.subject_id IN (SELECT id FROM entities WHERE entity_id = related_entity)
              OR t.object_id IN (SELECT id FROM entities WHERE entity_id = related_entity)
          )
    ),
    all_matches AS (
        SELECT * FROM semantic_matches
        UNION
        SELECT * FROM temporal_matches
        UNION
        SELECT * FROM spatial_matches
        UNION
        SELECT * FROM graph_matches
    ),
    scored_matches AS (
        SELECT 
            am.*,
            COUNT(*) OVER (PARTITION BY am.result_id) AS condition_count,
            ROW_NUMBER() OVER (
                PARTITION BY am.result_id 
                ORDER BY am.distance, condition_count DESC
            ) AS rn
        FROM all_matches am
    )
    SELECT 
        sm.result_type,
        sm.result_id,
        -- 根据类型构建结果 JSONB
        CASE sm.result_type
            WHEN 'entity' THEN (
                SELECT jsonb_build_object(
                    'entity_id', e.entity_id,
                    'name', e.name,
                    'type', e.entity_type,
                    'valid_from', e.valid_from,
                    'valid_to', e.valid_to,
                    'location', e.location
                )
                FROM entities e WHERE e.id = sm.result_id
            )
            WHEN 'triple' THEN (
                SELECT jsonb_build_object(
                    'subject', subj.name,
                    'predicate', t.predicate,
                    'object', obj.name,
                    'confidence', t.confidence
                )
                FROM triples t
                JOIN entities subj ON t.subject_id = subj.id
                LEFT JOIN entities obj ON t.object_id = obj.id
                WHERE t.id = sm.result_id
            )
            ELSE '{}'::JSONB
        END AS result_data,
        -- 综合得分: 条件匹配数 / (距离 + 0.01)
        (sm.condition_count / NULLIF(sm.distance + 0.01, 0))::DECIMAL AS relevance_score,
        to_jsonb(sm.matched_conditions) AS matched_conditions
    FROM scored_matches sm
    WHERE sm.rn = 1
    ORDER BY relevance_score DESC
    LIMIT limit_count
    OFFSET offset_start;
END;
$$ LANGUAGE plpgsql;
```

---

## 8. Python 集成实现

```python
"""
PostgreSQL 时空图谱因果系统 - Python 客户端
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict, Any
from uuid import UUID
import psycopg2
from psycopg2.extras import Json, register_uuid
import numpy as np

class CertaintyLevel(Enum):
    """确定性级别"""
    EXACT = "exact"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"

@dataclass
class TimeRange:
    """时间范围（支持模糊）"""
    exact_start: Optional[datetime] = None
    exact_end: Optional[datetime] = None
    
    # 模糊时间
    approx_start: Optional[datetime] = None
    approx_end: Optional[datetime] = None
    certainty: CertaintyLevel = CertaintyLevel.EXACT
    
    @property
    def sql_range(self) -> str:
        """生成 SQL 时间范围"""
        if self.certainty == CertaintyLevel.EXACT:
            start = self.exact_start or datetime.min
            end = self.exact_end or datetime.max
        else:
            start = self.approx_start or datetime.min
            end = self.approx_end or datetime.max
        
        return f"[{start.isoformat()}, {end.isoformat()}]"

@dataclass 
class FuzzyLocation:
    """模糊位置"""
    exact_coords: Optional[tuple[float, float]] = None  # (lng, lat)
    hierarchy: List[str] = field(default_factory=list)  # ["家", "客厅"]
    region: Optional[str] = None
    location_id: Optional[UUID] = None
    certainty: CertaintyLevel = CertaintyLevel.EXACT

class SpatioTemporalMemory:
    """
    时空图谱因果记忆系统
    
    基于 PostgreSQL 实现:
    - 时序索引 (tstzrange)
    - 空间索引 (PostGIS)
    - 图谱遍历 (递归 CTE)
    - 因果追溯
    """
    
    def __init__(
        self,
        host: str = "localhost",
        port: int = 5432,
        database: str = "memory",
        user: str = "postgres",
        password: str = ""
    ):
        """初始化连接"""
        self.conn = psycopg2.connect(
            host=host,
            port=port,
            database=database,
            user=user,
            password=password
        )
        self.conn.autocommit = True
        register_uuid()
    
    def close(self):
        """关闭连接"""
        self.conn.close()
    
    # ==================== 实体操作 ====================
    
    def add_entity(
        self,
        entity_id: str,
        entity_type: str,
        name: str,
        time_range: Optional[TimeRange] = None,
        location: Optional[FuzzyLocation] = None,
        metadata: Optional[Dict] = None,
        embedding: Optional[np.ndarray] = None
    ) -> UUID:
        """
        添加实体
        
        Args:
            entity_id: 归一化 ID
            entity_type: 实体类型 (person, location, object, concept, event)
            name: 原始名称
            time_range: 时间范围
            location: 位置信息
            metadata: 元数据
            embedding: 向量表示
            
        Returns:
            实体 UUID
        """
        cursor = self.conn.cursor()
        
        try:
            # 处理时间
            valid_from = time_range.exact_start if time_range else datetime.now()
            valid_to = time_range.exact_end if time_range else None
            time_certainty = time_range.certainty.value if time_range else "exact"
            
            # 处理位置
            location_geom = None
            location_fuzzy = None
            location_uuid = None
            
            if location:
                if location.exact_coords:
                    # PostGIS 坐标
                    location_geom = f"ST_SetSRID(ST_MakePoint({location.exact_coords[0]}, {location.exact_coords[1]}), 4326)"
                if location.hierarchy:
                    location_fuzzy = Json({"hierarchy": location.hierarchy})
                location_uuid = location.location_id
            
            # 处理向量
            embedding_arr = embedding.tolist() if embedding is not None else None
            
            query = """
                INSERT INTO entities (
                    entity_id, entity_type, name, name_normalized,
                    valid_from, valid_to, time_certainty,
                    location, location_fuzzy, location_id,
                    metadata, embedding
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    ST_GeomFromText(%s, 4326) %s, %s,
                    %s, %s
                )
                RETURNING id
            """.format(
                "NULL" if location_geom is None else location_geom,
                "NULL" if embedding_arr is None else "%s"
            )
            
            # 构建参数
            params = [
                entity_id, entity_type, name, name.lower().strip(),
                valid_from, valid_to, time_certainty,
                location_fuzzy, location_uuid,
                Json(metadata or {}),
            ]
            if embedding_arr:
                params.append(embedding_arr)
            
            cursor.execute(query, params)
            result = cursor.fetchone()
            return result[0]
            
        finally:
            cursor.close()
    
    def add_triple(
        self,
        subject_id: UUID,
        predicate: str,
        object_id: Optional[UUID] = None,
        object_value: Optional[str] = None,
        predecessor_id: Optional[UUID] = None,
        time_range: Optional[TimeRange] = None,
        confidence: float = 1.0,
        certainty: CertaintyLevel = CertaintyLevel.EXACT,
        relation_type: str = "semantic"
    ) -> UUID:
        """
        添加三元组关系
        
        Args:
            subject_id: 主语实体 UUID
            predicate: 关系类型
            object_id: 宾语实体 UUID (可选)
            object_value: 宾语值 (当没有 object_id 时)
            predecessor_id: 前因三元组 UUID
            time_range: 时间范围
            confidence: 置信度 0-1
            certainty: 确定性级别
            relation_type: 关系类型 (causal, spatial, temporal, semantic)
            
        Returns:
            三元组 UUID
        """
        cursor = self.conn.cursor()
        
        try:
            valid_from = time_range.exact_start if time_range else datetime.now()
            valid_to = time_range.exact_end if time_range else None
            
            query = """
                INSERT INTO triples (
                    triple_id, subject_id, predicate, object_id, object_value,
                    valid_from, valid_to, time_certainty,
                    predecessor_id, confidence, certainty, relation_type
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s
                )
                RETURNING id
            """
            
            triple_id = f"{subject_id}_{predicate}_{object_id or 'value'}"
            params = [
                triple_id, subject_id, predicate, object_id, object_value,
                valid_from, valid_to, certainty.value,
                predecessor_id, confidence, certainty.value, relation_type
            ]
            
            cursor.execute(query, params)
            result = cursor.fetchone()
            
            # 更新后继列表
            if predecessor_id:
                self._update_successors(predecessor_id, result[0])
            
            return result[0]
            
        finally:
            cursor.close()
    
    def _update_successors(self, predecessor_id: UUID, new_successor_id: UUID):
        """更新后继列表"""
        cursor = self.conn.cursor()
        try:
            cursor.execute("""
                UPDATE triples 
                SET successor_ids = array_append(successor_ids, %s)
                WHERE id = %s
            """, [str(new_successor_id), str(predecessor_id)])
        finally:
            cursor.close()
    
    # ==================== 检索操作 ====================
    
    def query_at_time(self, query_time: datetime, entity_type: Optional[str] = None) -> List[Dict]:
        """
        查询某个时间点的记忆
        
        Args:
            query_time: 查询时间点
            entity_type: 过滤实体类型
            
        Returns:
            实体列表
        """
        cursor = self.conn.cursor(name="query_cursor")
        cursor.itersize = 100
        
        try:
            query = """
                SELECT entity_id, name, entity_type, 
                       valid_from, valid_to, metadata
                FROM entities
                WHERE valid_from <= %s
                  AND (valid_to IS NULL OR valid_to > %s)
            """
            params = [query_time, query_time]
            
            if entity_type:
                query += " AND entity_type = %s"
                params.append(entity_type)
            
            query += " ORDER BY entity_type, name"
            
            cursor.execute(query, params)
            
            results = []
            for row in cursor:
                results.append({
                    "entity_id": row[0],
                    "name": row[1],
                    "entity_type": row[2],
                    "valid_from": row[3],
                    "valid_to": row[4],
                    "metadata": row[5]
                })
            
            return results
            
        finally:
            cursor.close()
    
    def query_in_range(
        self, 
        start_time: datetime, 
        end_time: datetime
    ) -> List[Dict]:
        """
        查询时间范围内的记忆
        
        Args:
            start_time: 范围起始
            end_time: 范围结束
            
        Returns:
            实体列表及重叠范围
        """
        cursor = self.conn.cursor(name="range_cursor")
        cursor.itersize = 100
        
        try:
            cursor.execute("""
                SELECT 
                    entity_id, name, entity_type,
                    GREATEST(valid_from, %s) as overlap_start,
                    LEAST(COALESCE(valid_to, %s), %s) as overlap_end
                FROM entities
                WHERE valid_from <= %s
                  AND (valid_to IS NULL OR valid_to >= %s)
                ORDER BY overlap_start
            """, [start_time, end_time, end_time, end_time, start_time])
            
            results = []
            for row in cursor:
                results.append({
                    "entity_id": row[0],
                    "name": row[1],
                    "entity_type": row[2],
                    "overlap_range": {"start": row[3], "end": row[4]}
                })
            
            return results
            
        finally:
            cursor.close()
    
    def query_in_area(
        self,
        lng: float,
        lat: float,
        radius_meters: int = 100
    ) -> List[Dict]:
        """
        查询空间范围内的实体
        
        Args:
            lng: 经度
            lat: 纬度
            radius_meters: 半径（米）
            
        Returns:
            实体列表及距离
        """
        cursor = self.conn.cursor()
        
        try:
            cursor.execute("""
                SELECT 
                    e.entity_id, e.name, e.entity_type,
                    ST_Distance(
                        e.location::geography,
                        ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography
                    ) as distance_meters
                FROM entities e
                WHERE e.location IS NOT NULL
                  AND ST_DWithin(
                      e.location::geography,
                      ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                      %s
                  )
                ORDER BY distance_meters
            """, [lng, lat, lng, lat, radius_meters])
            
            results = []
            for row in cursor:
                results.append({
                    "entity_id": row[0],
                    "name": row[1],
                    "entity_type": row[2],
                    "distance_meters": row[3]
                })
            
            return results
            
        finally:
            cursor.close()
    
    def trace_causal_chain(
        self,
        event_id: UUID,
        direction: str = "backward",
        max_depth: int = 10
    ) -> List[Dict]:
        """
        追溯因果链
        
        Args:
            event_id: 起始事件 UUID
            direction: 'backward'=原因, 'forward'=结果
            max_depth: 最大追溯深度
            
        Returns:
            因果链节点列表
        """
        cursor = self.conn.cursor()
        
        try:
            results = []
            current_id = event_id
            accumulated_confidence = 1.0
            depth = 0
            
            while current_id and depth < max_depth:
                # 获取当前三元组及关联实体
                cursor.execute("""
                    SELECT 
                        t.id, t.subject_id, t.predicate, t.object_id,
                        t.confidence, t.valid_from,
                        e_subj.name as subject_name,
                        e_obj.name as object_name
                    FROM triples t
                    JOIN entities e_subj ON t.subject_id = e_subj.id
                    LEFT JOIN entities e_obj ON t.object_id = e_obj.id
                    WHERE t.id = %s
                """, [str(current_id)])
                
                row = cursor.fetchone()
                if not row:
                    break
                
                accumulated_confidence *= row[4]
                
                results.append({
                    "depth": depth,
                    "triple_id": row[0],
                    "subject": row[6],
                    "predicate": row[2],
                    "object": row[7],
                    "confidence": row[4],
                    "accumulated_confidence": accumulated_confidence,
                    "time": row[5]
                })
                
                # 获取下一个节点
                if direction == "backward":
                    cursor.execute(
                        "SELECT predecessor_id FROM triples WHERE id = %s",
                        [str(current_id)]
                    )
                else:
                    cursor.execute(
                        "SELECT successor_ids[1] FROM triples WHERE id = %s",
                        [str(current_id)]
                    )
                
                next_row = cursor.fetchone()
                current_id = next_row[0] if next_row else None
                depth += 1
            
            return results
            
        finally:
            cursor.close()
    
    def find_path(
        self,
        start_entity_id: str,
        end_entity_id: str,
        max_depth: int = 5
    ) -> List[Dict]:
        """
        查找两实体间的路径
        
        Args:
            start_entity_id: 起始实体 ID
            end_entity_id: 目标实体 ID
            max_depth: 最大深度
            
        Returns:
            路径列表
        """
        cursor = self.conn.cursor()
        
        try:
            cursor.execute("""
                WITH RECURSIVE path_search AS (
                    -- 起始节点
                    SELECT 
                        ARRAY[e_start.id] AS path,
                        e_start.id AS current_id,
                        0::DECIMAL AS total_weight,
                        1 AS depth,
                        ARRAY[e_start.entity_id] AS entity_ids
                    FROM entities e_start
                    WHERE e_start.entity_id = %s
                    
                    UNION ALL
                    
                    -- 递归扩展
                    SELECT 
                        ps.path || t.object_id,
                        t.object_id,
                        ps.total_weight + COALESCE(t.edge_weight, 1),
                        ps.depth + 1,
                        ps.entity_ids || e_rel.entity_id
                    FROM path_search ps
                    JOIN triples t ON t.subject_id = ps.current_id
                    LEFT JOIN entities e_rel ON t.object_id = e_rel.id
                    WHERE ps.depth < %s
                      AND t.object_id IS NOT NULL
                      AND NOT t.object_id = ANY(ps.path)
                )
                SELECT 
                    entity_ids,
                    total_weight,
                    depth - 1 as path_length
                FROM path_search
                WHERE current_id IN (
                    SELECT id FROM entities WHERE entity_id = %s
                )
                ORDER BY total_weight
                LIMIT 1
            """, [start_entity_id, max_depth, end_entity_id])
            
            row = cursor.fetchone()
            if row:
                return {
                    "path": row[0],
                    "total_weight": row[1],
                    "path_length": row[2]
                }
            return None
            
        finally:
            cursor.close()
    
    def hybrid_search(
        self,
        query_embedding: Optional[np.ndarray] = None,
        time_range: Optional[TimeRange] = None,
        location: Optional[FuzzyLocation] = None,
        related_entity: Optional[str] = None,
        min_confidence: float = 0.5,
        limit: int = 20
    ) -> List[Dict]:
        """
        混合检索
        
        Args:
            query_embedding: 查询向量
            time_range: 时间范围
            location: 空间位置
            related_entity: 关联实体 ID
            min_confidence: 最小置信度
            limit: 返回数量
            
        Returns:
            检索结果列表
        """
        cursor = self.conn.cursor()
        
        try:
            # 构建查询条件
            conditions = []
            params = []
            param_idx = 1
            
            # 语义条件
            if query_embedding is not None:
                conditions.append(f"""
                    vl.entity_id IS NOT NULL
                """)
            
            # 时间条件
            if time_range:
                if time_range.exact_start:
                    conditions.append(f"e.valid_from <= %s")
                    params.append(time_range.exact_end or datetime.max)
                if time_range.exact_end:
                    conditions.append(f"(e.valid_to >= %s OR e.valid_to IS NULL)")
                    params.append(time_range.exact_start or datetime.min)
            
            # 空间条件
            if location and location.exact_coords:
                lng, lat = location.exact_coords
                conditions.append(f"""
                    ST_DWithin(
                        e.location::geography,
                        ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                        %s
                    )
                """)
                params.extend([lng, lat, 100])  # 默认 100 米
            
            # 构建完整查询
            query = f"""
                SELECT 
                    e.entity_id,
                    e.name,
                    e.entity_type,
                    e.metadata,
                    e.valid_from,
                    e.valid_to
                FROM entities e
                LEFT JOIN vector_links vl ON vl.entity_id = e.id
                WHERE 1=1
            """
            
            if conditions:
                query += " AND " + " AND ".join(conditions)
            
            if min_confidence < 1.0:
                query += f" AND e.metadata->>'confidence' >= '{min_confidence}'"
            
            query += " ORDER BY e.updated_at DESC LIMIT %s"
            params.append(limit)
            
            cursor.execute(query, params)
            
            results = []
            for row in cursor:
                results.append({
                    "entity_id": row[0],
                    "name": row[1],
                    "entity_type": row[2],
                    "metadata": row[3],
                    "valid_from": row[4],
                    "valid_to": row[5]
                })
            
            return results
            
        finally:
            cursor.close()


# ==================== 使用示例 ====================

def example_usage():
    """使用示例"""
    
    # 初始化连接
    memory = SpatioTemporalMemory(
        host="localhost",
        database="agent_memory"
    )
    
    # 1. 添加实体
    person_id = memory.add_entity(
        entity_id="xiaoming",
        entity_type="person",
        name="小明",
        time_range=TimeRange(
            exact_start=datetime(2024, 1, 1)
        ),
        location=FuzzyLocation(
            hierarchy=["家", "客厅"]
        )
    )
    
    # 2. 添加另一个实体
    location_id = memory.add_entity(
        entity_id="home_living_room",
        entity_type="location",
        name="客厅",
        time_range=TimeRange(
            exact_start=datetime(2024, 1, 1)
        )
    )
    
    # 3. 添加关系: 小明 -> 位于 -> 客厅
    triple_id = memory.add_triple(
        subject_id=person_id,
        predicate="位于",
        object_id=location_id,
        time_range=TimeRange(
            exact_start=datetime(2024, 6, 1),
            exact_end=datetime(2024, 6, 30)
        ),
        relation_type="spatial"
    )
    
    # 4. 添加因果链: 生病 -> 导致 -> 心情不好
    sick_id = memory.add_entity(
        entity_id="sick",
        entity_type="event",
        name="感冒"
    )
    
    sad_id = memory.add_entity(
        entity_id="sad",
        entity_type="state",
        name="心情不好"
    )
    
    causal_id = memory.add_triple(
        subject_id=sick_id,
        predicate="导致",
        object_id=sad_id,
        relation_type="causal",
        confidence=0.9
    )
    
    # 5. 时序查询
    current_entities = memory.query_at_time(datetime.now())
    print(f"当前实体数: {len(current_entities)}")
    
    # 6. 空间查询
    nearby = memory.query_in_area(lng=116.4, lat=39.9, radius_meters=100)
    print(f"100米内实体数: {len(nearby)}")
    
    # 7. 因果追溯
    causes = memory.trace_causal_chain(sad_id, direction="backward")
    print(f"心情不好的原因链: {causes}")
    
    # 8. 混合检索
    results = memory.hybrid_search(
        time_range=TimeRange(
            exact_start=datetime(2024, 6, 1),
            exact_end=datetime(2024, 6, 30)
        )
    )
    print(f"混合检索结果: {len(results)}")
    
    memory.close()


if __name__ == "__main__":
    example_usage()
```

---

## 9. 性能优化建议

### 9.1 索引策略

```sql
-- 时序分区（按月）
CREATE TABLE entities_2024_06 PARTITION OF entities
    FOR VALUES FROM ('2024-06-01') TO ('2024-07-01');

-- 复合索引
CREATE INDEX idx_entities_type_time ON entities(entity_type, valid_from, valid_to);
CREATE INDEX idx_triples_subject_pred ON triples(subject_id, predicate);
CREATE INDEX idx_triples_confidence ON triples(confidence) WHERE confidence < 1.0;

-- GIN 索引（JSONB）
CREATE INDEX idx_entities_metadata ON entities USING GIN(metadata);
CREATE INDEX idx_triples_metadata ON triples USING GIN(metadata);
```

### 9.2 连接池配置

```python
# psycopg2 连接池
from psycopg2.pool import ThreadedConnectionPool

pool = ThreadedConnectionPool(
    minconn=5,
    maxconn=20,
    host="localhost",
    database="memory",
    user="postgres"
)
```

---

## 10. 总结

| 特性 | PostgreSQL 实现 | 优势 |
|------|----------------|------|
| 时序索引 | tstzrange + GiST | 原生支持范围查询 |
| 空间索引 | PostGIS | 成熟的 GIS 解决方案 |
| 图谱遍历 | 递归 CTE | 替代 Neo4j，无需额外依赖 |
| 因果追溯 | predecessor_id 链 | 灵活支持模糊因果 |
| 向量检索 | pgvector | 混合检索一体化 |
| 模糊查询 | JSONB + 自定义函数 | 支持不确定输入 |

这套设计的核心理念：**用 PostgreSQL 的扩展能力，替代多个专用数据库，实现真正的 All-in-One 记忆系统**。
