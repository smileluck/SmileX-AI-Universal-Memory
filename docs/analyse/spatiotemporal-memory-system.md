# 时空记忆体：完整实现方案

> 创建日期：2026-05-25
> 目标：构建一个时空结合的记忆系统，覆盖个人/项目/企业/AI Agent 多种场景
> 架构：混合架构（TimescaleDB + PostGIS + ChromaDB + Neo4j）

---

## 一、核心概念

### 什么是时空记忆体？

```
传统记忆： "团队决定使用 GraphQL"
          ↓
          只有内容维度

时空记忆： "2023年6月15日 14:30，在北京办公室，团队决定使用 GraphQL"
          ↓
          时间维度 ──┐
          空间维度 ──┼→ 完整的时空上下文
          内容维度 ──┘
```

### 时空记忆体的价值

| 查询类型 | 传统记忆 | 时空记忆 |
|---------|---------|---------|
| "GraphQL 相关决策" | ✅ 能查到 | ✅ 能查到 |
| "去年这时候在做什么" | ❌ 查不到 | ✅ 时间序列查询 |
| "在北京的项目有哪些" | ❌ 查不到 | ✅ 空间范围查询 |
| "项目演进历史" | ❌ 碎片化 | ✅ 时间线可视化 |
| "同一地点的决策" | ❌ 查不到 | ✅ 空间聚合分析 |

---

## 二、混合架构设计

```
┌─────────────────────────────────────────────────────────────────┐
│                    时空记忆体混合架构                             │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐ │
│  │                    应用层（API / SDK）                      │ │
│  │  · 时空查询接口（时间范围、空间范围、时空交集）               │ │
│  │  · 时间线生成（个人/项目/地点）                              │ │
│  │  · 时空可视化（地图 + 时间轴）                               │ │
│  └───────────────────────────────────────────────────────────┘ │
│                              │                                  │
│  ┌───────────────────────────┼───────────────────────────────┐ │
│  │                      融合检索层                             │ │
│  │  · 时空索引（R-tree + B-tree）                              │ │
│  │  · 语义检索（向量相似度）                                    │ │
│  │  · 混合排序（时间衰减 + 空间距离 + 语义相关）                 │ │
│  └───────────────────────────┼───────────────────────────────┘ │
│                              │                                  │
│  ┌──────────────┬────────────┼────────────┬──────────────────┐ │
│  │   时序存储    │   空间存储  │   向量存储  │     图存储        │ │
│  │  TimescaleDB │  PostGIS   │  ChromaDB  │    Neo4j         │ │
│  │              │            │            │                  │ │
│  │ · 时间范围查询│ · 空间范围查询│ · 语义相似度 │ · 关系遍历       │ │
│  │ · 聚合统计   │ · 距离计算  │ · 内容检索   │ · 因果链追踪     │ │
│  │ · 降采样    │ · 地理围栏  │ · 模糊匹配   │ · 网络分析       │ │
│  └──────────────┴────────────┴────────────┴──────────────────┘ │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## 三、数据模型

### 3.1 核心实体定义

```python
# models/spatiotemporal_memory.py

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from enum import Enum
import uuid


class TemporalGranularity(Enum):
    """时间粒度"""
    MOMENT = "moment"       # 瞬间（精确到秒）
    PERIOD = "period"       # 时段（有开始和结束）
    RECURRING = "recurring" # 周期性（每周会议）


class SpatialGranularity(Enum):
    """空间粒度"""
    POINT = "point"         # 精确坐标（GPS）
    REGION = "region"       # 区域（办公室、城市）
    VIRTUAL = "virtual"     # 虚拟空间（项目阶段、版本）


@dataclass
class TemporalContext:
    """时间上下文"""
    # 核心时间
    timestamp: datetime                     # 精确时间点
    granularity: TemporalGranularity = TemporalGranularity.MOMENT
    
    # 时段记忆（如会议、项目周期）
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration: Optional[timedelta] = None
    
    # 时间特征（用于快速过滤）
    year: int = None
    month: int = None
    day: int = None
    weekday: int = None           # 0=周一
    hour: int = None
    
    # 相对时间描述
    temporal_tags: List[str] = None  # ["morning", "weekend", "holiday"]
    
    def __post_init__(self):
        if self.year is None:
            self.year = self.timestamp.year
            self.month = self.timestamp.month
            self.day = self.timestamp.day
            self.weekday = self.timestamp.weekday()
            self.hour = self.timestamp.hour


@dataclass
class SpatialContext:
    """空间上下文"""
    # 空间标识
    location_id: str                    # 地点唯一ID
    location_name: str                  # 地点名称（如"北京办公室"）
    granularity: SpatialGranularity = SpatialGranularity.REGION
    
    # 物理坐标（可选）
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude: Optional[float] = None
    
    # 空间层级（用于范围查询）
    country: Optional[str] = None
    province: Optional[str] = None
    city: Optional[str] = None
    district: Optional[str] = None
    building: Optional[str] = None
    floor: Optional[str] = None
    room: Optional[str] = None
    
    # 虚拟空间（项目/版本等非物理空间）
    virtual_space: Optional[str] = None   # "project-driftwood/v2.0"
    virtual_path: Optional[str] = None    # "backend/auth-module"


@dataclass
class SpatiotemporalMemory:
    """时空记忆单元"""
    # 基础标识
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    content: str = ""                    # 记忆内容
    
    # 时空上下文
    temporal: TemporalContext = None
    spatial: SpatialContext = None
    
    # 语义向量（用于内容检索）
    embedding: Optional[List[float]] = None
    
    # 关系连接（图结构）
    related_memories: List[str] = None   # 相关记忆ID
    causal_predecessors: List[str] = None  # 因果前驱
    causal_successors: List[str] = None    # 因果后继
    
    # 元数据
    category: str = "fact"               # fact/event/decision/observation
    importance: float = 0.5
    source: str = ""                     # 来源
    tags: List[str] = None
    
    # 创建信息
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
```

---

## 四、分层存储实现

### 4.1 时序存储（TimescaleDB）

```python
# storage/temporal_store.py

import asyncpg
from datetime import datetime, timedelta
from typing import List, Dict, Optional


class TemporalStore:
    """
    时序数据存储 - TimescaleDB
    负责：时间范围查询、时间聚合、时序分析
    """
    
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool = None
    
    async def init(self):
        """初始化数据库和 hypertable"""
        self.pool = await asyncpg.create_pool(self.dsn)
        
        async with self.pool.acquire() as conn:
            # 创建主表
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS temporal_memories (
                    id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL,
                    timestamp TIMESTAMPTZ NOT NULL,
                    start_time TIMESTAMPTZ,
                    end_time TIMESTAMPTZ,
                    year INTEGER,
                    month INTEGER,
                    day INTEGER,
                    weekday INTEGER,
                    hour INTEGER,
                    content TEXT,
                    importance FLOAT,
                    location_id TEXT,
                    metadata JSONB
                );
            """)
            
            # 转换为 hypertable（TimescaleDB 特性）
            await conn.execute("""
                SELECT create_hypertable('temporal_memories', 'timestamp', 
                                         if_not_exists => TRUE);
            """)
            
            # 创建索引
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_temp_time_range 
                ON temporal_memories (start_time, end_time);
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_temp_location 
                ON temporal_memories (location_id, timestamp);
            """)
    
    async def store(self, memory: 'SpatiotemporalMemory'):
        """存储时序数据"""
        temp = memory.temporal
        
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO temporal_memories 
                (id, memory_id, timestamp, start_time, end_time,
                 year, month, day, weekday, hour,
                 content, importance, location_id, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                ON CONFLICT (id) DO UPDATE SET
                    content = EXCLUDED.content,
                    importance = EXCLUDED.importance,
                    metadata = EXCLUDED.metadata;
            """, 
                memory.id,
                memory.id,
                temp.timestamp,
                temp.start_time,
                temp.end_time,
                temp.year,
                temp.month,
                temp.day,
                temp.weekday,
                temp.hour,
                memory.content,
                memory.importance,
                memory.spatial.location_id if memory.spatial else None,
                {"category": memory.category, "tags": memory.tags or []}
            )
    
    async def query_time_range(
        self,
        start: datetime,
        end: datetime,
        location_id: str = None,
        limit: int = 100,
    ) -> List[Dict]:
        """
        时间范围查询
        示例："2023年6月的所有记忆"
        """
        async with self.pool.acquire() as conn:
            if location_id:
                rows = await conn.fetch("""
                    SELECT * FROM temporal_memories
                    WHERE timestamp BETWEEN $1 AND $2
                      AND location_id = $3
                    ORDER BY timestamp DESC
                    LIMIT $4;
                """, start, end, location_id, limit)
            else:
                rows = await conn.fetch("""
                    SELECT * FROM temporal_memories
                    WHERE timestamp BETWEEN $1 AND $2
                    ORDER BY timestamp DESC
                    LIMIT $3;
                """, start, end, limit)
            
            return [dict(row) for row in rows]
    
    async def query_temporal_pattern(
        self,
        pattern: str,  # "morning", "weekend", "holiday"
        year: int = None,
    ) -> List[Dict]:
        """
        时间模式查询
        示例："所有周末的记忆"
        """
        conditions = {
            "morning": "hour BETWEEN 6 AND 11",
            "afternoon": "hour BETWEEN 12 AND 17",
            "evening": "hour BETWEEN 18 AND 23",
            "weekend": "weekday IN (5, 6)",
            "weekday": "weekday IN (0, 1, 2, 3, 4)",
        }
        
        where_clause = conditions.get(pattern, "TRUE")
        
        async with self.pool.acquire() as conn:
            sql = f"""
                SELECT * FROM temporal_memories
                WHERE {where_clause}
                {"AND year = $1" if year else ""}
                ORDER BY timestamp DESC;
            """
            
            if year:
                rows = await conn.fetch(sql, year)
            else:
                rows = await conn.fetch(sql)
            
            return [dict(row) for row in rows]
    
    async def get_timeline(
        self,
        group_by: str = "day",
        start: datetime = None,
        end: datetime = None,
    ) -> List[Dict]:
        """
        生成时间线聚合
        示例："按天统计记忆数量"
        """
        time_bucket = {
            "hour": "1 hour",
            "day": "1 day",
            "week": "1 week",
            "month": "1 month",
        }.get(group_by, "1 day")
        
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(f"""
                SELECT 
                    time_bucket($1, timestamp) as period,
                    COUNT(*) as count,
                    AVG(importance) as avg_importance,
                    array_agg(content) as contents
                FROM temporal_memories
                WHERE timestamp BETWEEN $2 AND $3
                GROUP BY period
                ORDER BY period;
            """, time_bucket, start or datetime.min, end or datetime.max)
            
            return [dict(row) for row in rows]
```

### 4.2 空间存储（PostGIS）

```python
# storage/spatial_store.py

import asyncpg
from datetime import datetime
from typing import List, Dict, Optional


class SpatialStore:
    """
    空间数据存储 - PostGIS
    负责：空间范围查询、距离计算、地理围栏
    """
    
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool = None
    
    async def init(self):
        """初始化 PostGIS 扩展"""
        self.pool = await asyncpg.create_pool(self.dsn)
        
        async with self.pool.acquire() as conn:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
            
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS spatial_memories (
                    id TEXT PRIMARY KEY,
                    memory_id TEXT NOT NULL,
                    location_id TEXT NOT NULL,
                    location_name TEXT,
                    country TEXT,
                    province TEXT,
                    city TEXT,
                    district TEXT,
                    building TEXT,
                    floor TEXT,
                    room TEXT,
                    virtual_space TEXT,
                    virtual_path TEXT,
                    geom GEOMETRY(POINT, 4326),
                    importance FLOAT,
                    content TEXT,
                    timestamp TIMESTAMPTZ
                );
            """)
            
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_spatial_geom 
                ON spatial_memories USING GIST(geom);
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_spatial_location 
                ON spatial_memories (location_id);
            """)
    
    async def store(self, memory: 'SpatiotemporalMemory'):
        """存储空间数据"""
        spatial = memory.spatial
        
        async with self.pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO spatial_memories 
                (id, memory_id, location_id, location_name,
                 country, province, city, district, building, floor, room,
                 virtual_space, virtual_path, geom,
                 importance, content, timestamp)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, 
                        ST_SetSRID(ST_MakePoint($14, $15), 4326),
                        $16, $17, $18)
                ON CONFLICT (id) DO UPDATE SET
                    content = EXCLUDED.content,
                    importance = EXCLUDED.importance;
            """,
                memory.id,
                memory.id,
                spatial.location_id,
                spatial.location_name,
                spatial.country,
                spatial.province,
                spatial.city,
                spatial.district,
                spatial.building,
                spatial.floor,
                spatial.room,
                spatial.virtual_space,
                spatial.virtual_path,
                spatial.longitude,
                spatial.latitude,
                memory.importance,
                memory.content,
                memory.temporal.timestamp if memory.temporal else None,
            )
    
    async def query_spatial_range(
        self,
        center_lat: float,
        center_lon: float,
        radius_meters: float,
        start_time: datetime = None,
        end_time: datetime = None,
    ) -> List[Dict]:
        """
        空间范围查询（圆形）
        示例："距离我 500 米内的所有记忆"
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT *,
                       ST_Distance(
                           geom::geography,
                           ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography
                       ) as distance_meters
                FROM spatial_memories
                WHERE ST_DWithin(
                    geom::geography,
                    ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography,
                    $3
                )
                AND ($4::timestamptz IS NULL OR timestamp >= $4)
                AND ($5::timestamptz IS NULL OR timestamp <= $5)
                ORDER BY distance_meters;
            """, center_lat, center_lon, radius_meters, start_time, end_time)
            
            return [dict(row) for row in rows]
    
    async def query_bounding_box(
        self,
        min_lat: float,
        min_lon: float,
        max_lat: float,
        max_lon: float,
    ) -> List[Dict]:
        """
        矩形范围查询
        示例："北京市区内的所有记忆"
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM spatial_memories
                WHERE geom && ST_MakeEnvelope($1, $2, $3, $4, 4326)
                ORDER BY timestamp DESC;
            """, min_lon, min_lat, max_lon, max_lat)
            
            return [dict(row) for row in rows]
    
    async def query_location_hierarchy(
        self,
        city: str = None,
        building: str = None,
        room: str = None,
    ) -> List[Dict]:
        """
        层级空间查询
        示例："北京办公室 3 楼的所有记忆"
        """
        conditions = []
        params = []
        
        if city:
            conditions.append("city = $1")
            params.append(city)
        if building:
            conditions.append("building = $2")
            params.append(building)
        if room:
            conditions.append("room = $3")
            params.append(room)
        
        where_clause = " AND ".join(conditions) if conditions else "TRUE"
        
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(f"""
                SELECT * FROM spatial_memories
                WHERE {where_clause}
                ORDER BY timestamp DESC;
            """, *params)
            
            return [dict(row) for row in rows]
```

### 4.3 向量存储（ChromaDB）

```python
# storage/semantic_store.py

import chromadb
from sentence_transformers import SentenceTransformer
from typing import List, Dict, Optional


class SemanticStore:
    """
    语义存储 - ChromaDB
    负责：内容语义检索、模糊匹配
    """
    
    def __init__(
        self,
        persist_dir: str = "./chroma_db",
        model_name: str = "all-MiniLM-L6-v2",
    ):
        self.client = chromadb.PersistentClient(path=persist_dir)
        self.collection = self.client.get_or_create_collection(
            name="spatiotemporal_memories",
            metadata={"hnsw:space": "cosine"},
        )
        self.model = SentenceTransformer(model_name)
    
    def store(self, memory: 'SpatiotemporalMemory'):
        """存储语义向量"""
        embedding = self.model.encode(memory.content).tolist()
        
        metadata = {
            "location_id": memory.spatial.location_id if memory.spatial else "",
            "city": memory.spatial.city if memory.spatial else "",
            "year": memory.temporal.year if memory.temporal else 0,
            "month": memory.temporal.month if memory.temporal else 0,
            "importance": memory.importance,
            "category": memory.category,
        }
        
        self.collection.add(
            ids=[memory.id],
            embeddings=[embedding],
            documents=[memory.content],
            metadatas=[metadata],
        )
    
    def search(
        self,
        query: str,
        top_k: int = 10,
        filters: dict = None,
    ) -> List[Dict]:
        """
        语义检索
        示例："和 GraphQL 相关的记忆"
        """
        query_embedding = self.model.encode(query).tolist()
        
        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=filters,
        )
        
        memories = []
        for i, doc_id in enumerate(results["ids"][0]):
            memories.append({
                "id": doc_id,
                "content": results["documents"][0][i],
                "metadata": results["metadatas"][0][i],
                "distance": results["distances"][0][i],
            })
        
        return memories
```

### 4.4 图存储（Neo4j）

```python
# storage/graph_store.py

from neo4j import AsyncGraphDatabase
from typing import List, Dict, Optional


class GraphStore:
    """
    图存储 - Neo4j
    负责：关系遍历、因果链追踪、网络分析
    """
    
    def __init__(self, uri: str, user: str, password: str):
        self.driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
    
    async def init(self):
        """初始化图 schema"""
        async with self.driver.session() as session:
            await session.run("""
                CREATE CONSTRAINT memory_id IF NOT EXISTS
                FOR (m:Memory) REQUIRE m.id IS UNIQUE;
            """)
            
            await session.run("""
                CREATE INDEX memory_time IF NOT EXISTS
                FOR (m:Memory) ON (m.timestamp);
            """)
    
    async def store(self, memory: 'SpatiotemporalMemory'):
        """存储记忆节点"""
        async with self.driver.session() as session:
            # 创建记忆节点
            await session.run("""
                MERGE (m:Memory {id: $id})
                SET m.content = $content,
                    m.timestamp = datetime($timestamp),
                    m.importance = $importance,
                    m.category = $category,
                    m.location_id = $location_id
            """, 
                id=memory.id,
                content=memory.content,
                timestamp=memory.temporal.timestamp.isoformat() if memory.temporal else None,
                importance=memory.importance,
                category=memory.category,
                location_id=memory.spatial.location_id if memory.spatial else None,
            )
            
            # 创建关系
            for related_id in (memory.related_memories or []):
                await session.run("""
                    MATCH (m1:Memory {id: $id1})
                    MATCH (m2:Memory {id: $id2})
                    MERGE (m1)-[:RELATED_TO]->(m2)
                """, id1=memory.id, id2=related_id)
            
            for predecessor_id in (memory.causal_predecessors or []):
                await session.run("""
                    MATCH (m1:Memory {id: $id1})
                    MATCH (m2:Memory {id: $id2})
                    MERGE (m1)-[:CAUSED]->(m2)
                """, id1=predecessor_id, id2=memory.id)
    
    async def find_causal_chain(
        self,
        memory_id: str,
        direction: str = "both",
        max_depth: int = 5,
    ) -> List[Dict]:
        """
        查找因果链
        示例："导致这个决策的所有前置事件"
        """
        async with self.driver.session() as session:
            if direction == "backward":
                result = await session.run("""
                    MATCH path = (predecessor)-[:CAUSED*1..$depth]->(m:Memory {id: $id})
                    RETURN [node in nodes(path) | node.id] as chain,
                           [node in nodes(path) | node.content] as contents
                    ORDER BY length(path)
                """, id=memory_id, depth=max_depth)
            
            elif direction == "forward":
                result = await session.run("""
                    MATCH path = (m:Memory {id: $id})-[:CAUSED*1..$depth]->(successor)
                    RETURN [node in nodes(path) | node.id] as chain,
                           [node in nodes(path) | node.content] as contents
                    ORDER BY length(path)
                """, id=memory_id, depth=max_depth)
            
            else:
                result = await session.run("""
                    MATCH path = (predecessor)-[:CAUSED*0..$depth]->(m:Memory {id: $id})-[:CAUSED*0..$depth]->(successor)
                    RETURN [node in nodes(path) | node.id] as chain,
                           [node in nodes(path) | node.content] as contents
                    ORDER BY length(path)
                """, id=memory_id, depth=max_depth)
            
            records = await result.data()
            return records
    
    async def find_temporal_neighbors(
        self,
        memory_id: str,
        time_window_hours: float = 24,
    ) -> List[Dict]:
        """
        查找时间邻近的记忆
        示例："这个事件发生前后 24 小时的记忆"
        """
        async with self.driver.session() as session:
            result = await session.run("""
                MATCH (m:Memory {id: $id})
                MATCH (neighbor:Memory)
                WHERE neighbor.id <> m.id
                  AND duration.between(m.timestamp, neighbor.timestamp).hours <= $window
                RETURN neighbor.id as id,
                       neighbor.content as content,
                       neighbor.timestamp as timestamp,
                       duration.between(m.timestamp, neighbor.timestamp).hours as time_diff
                ORDER BY abs(time_diff)
            """, id=memory_id, window=time_window_hours)
            
            return await result.data()
```

---

## 五、统一查询接口

```python
# core/spatiotemporal_engine.py

from typing import List, Optional
from datetime import datetime, timedelta


class SpatiotemporalQuery:
    """时空查询参数"""
    
    def __init__(
        self,
        # 时间条件
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        temporal_pattern: Optional[str] = None,
        
        # 空间条件
        center_lat: Optional[float] = None,
        center_lon: Optional[float] = None,
        radius_meters: Optional[float] = None,
        location_id: Optional[str] = None,
        city: Optional[str] = None,
        
        # 语义条件
        content_query: Optional[str] = None,
        
        # 组合策略
        strategy: str = "intersection",
    ):
        self.start_time = start_time
        self.end_time = end_time
        self.temporal_pattern = temporal_pattern
        self.center_lat = center_lat
        self.center_lon = center_lon
        self.radius_meters = radius_meters
        self.location_id = location_id
        self.city = city
        self.content_query = content_query
        self.strategy = strategy


class SpatiotemporalEngine:
    """
    时空记忆引擎 - 统一查询接口
    """
    
    def __init__(
        self,
        temporal_store: 'TemporalStore',
        spatial_store: 'SpatialStore',
        semantic_store: 'SemanticStore',
        graph_store: 'GraphStore',
    ):
        self.temporal = temporal_store
        self.spatial = spatial_store
        self.semantic = semantic_store
        self.graph = graph_store
    
    async def query(self, q: SpatiotemporalQuery) -> List['SpatiotemporalMemory']:
        """
        执行时空查询
        """
        if q.strategy == "temporal_first":
            return await self._temporal_first_strategy(q)
        elif q.strategy == "spatial_first":
            return await self._spatial_first_strategy(q)
        elif q.strategy == "semantic_first":
            return await self._semantic_first_strategy(q)
        else:
            return await self._intersection_strategy(q)
    
    async def _temporal_first_strategy(self, q: SpatiotemporalQuery) -> List['SpatiotemporalMemory']:
        """时间优先策略"""
        temporal_results = await self.temporal.query_time_range(
            start=q.start_time or datetime.min,
            end=q.end_time or datetime.max,
            location_id=q.location_id,
            limit=1000,
        )
        
        memory_ids = [r["memory_id"] for r in temporal_results]
        
        if q.center_lat and q.center_lon:
            spatial_results = await self.spatial.query_spatial_range(
                center_lat=q.center_lat,
                center_lon=q.center_lon,
                radius_meters=q.radius_meters or 1000,
            )
            spatial_ids = {r["memory_id"] for r in spatial_results}
            memory_ids = [mid for mid in memory_ids if mid in spatial_ids]
        
        return await self._fetch_full_memories(memory_ids[:50])
    
    async def get_timeline(
        self,
        location_id: str = None,
        group_by: str = "day",
    ) -> dict:
        """
        生成时空时间线
        """
        timeline = await self.temporal.get_timeline(group_by=group_by)
        
        return {
            "timeline": timeline,
            "total_memories": sum(p["count"] for p in timeline),
            "time_range": {
                "start": timeline[0]["period"] if timeline else None,
                "end": timeline[-1]["period"] if timeline else None,
            },
        }
```

---

## 六、使用示例

```python
# 使用示例

async def main():
    """时空记忆体使用示例"""
    
    # 初始化存储
    temporal = TemporalStore("postgresql://localhost/temporal_db")
    spatial = SpatialStore("postgresql://localhost/spatial_db")
    semantic = SemanticStore("./chroma_db")
    graph = GraphStore("bolt://localhost:7687", "neo4j", "password")
    
    # 初始化
    await temporal.init()
    await spatial.init()
    await graph.init()
    
    # 创建引擎
    engine = SpatiotemporalEngine(temporal, spatial, semantic, graph)
    
    # 创建时空记忆
    memory = SpatiotemporalMemory(
        content="团队决定使用 GraphQL 替代 REST",
        temporal=TemporalContext(
            timestamp=datetime(2023, 6, 15, 14, 30),
        ),
        spatial=SpatialContext(
            location_id="beijing-office-3f",
            location_name="北京办公室 3 楼会议室",
            city="北京",
            building="望京 SOHO",
            floor="3F",
            room="会议室 A",
            latitude=39.999,
            longitude=116.481,
        ),
        importance=0.9,
        category="decision",
    )
    
    # 存储到所有存储层
    await temporal.store(memory)
    await spatial.store(memory)
    await semantic.store(memory)
    await graph.store(memory)
    
    # 查询示例
    
    # 1. "2023年6月，在北京发生的所有决策"
    results = await engine.query(SpatiotemporalQuery(
        start_time=datetime(2023, 6, 1),
        end_time=datetime(2023, 6, 30),
        city="北京",
    ))
    
    # 2. "距离我当前位置 500 米内的记忆"
    results = await engine.query(SpatiotemporalQuery(
        center_lat=39.999,
        center_lon=116.481,
        radius_meters=500,
    ))
    
    # 3. "和 GraphQL 相关的记忆，在 2023 年"
    results = await engine.query(SpatiotemporalQuery(
        content_query="GraphQL",
        start_time=datetime(2023, 1, 1),
        end_time=datetime(2023, 12, 31),
        strategy="semantic_first",
    ))
    
    # 4. 生成个人时间线
    timeline = await engine.get_timeline(group_by="week")
    
    # 5. 查找因果链
    chain = await graph.find_causal_chain(
        memory_id="some-decision-id",
        direction="backward",
        max_depth=3,
    )


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

---

## 七、部署配置

### docker-compose.yml

```yaml
version: '3.8'

services:
  # 时序存储
  timescale:
    image: timescale/timescaledb:latest-pg15
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: password
      POSTGRES_DB: temporal_db
    ports:
      - "5432:5432"
    volumes:
      - timescale_data:/var/lib/postgresql/data

  # 空间存储（PostGIS）
  postgis:
    image: postgis/postgis:15-3.4
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: password
      POSTGRES_DB: spatial_db
    ports:
      - "5433:5432"
    volumes:
      - postgis_data:/var/lib/postgresql/data

  # 向量存储
  chroma:
    image: ghcr.io/chroma-core/chroma:latest
    ports:
      - "8000:8000"
    volumes:
      - chroma_data:/chroma/chroma

  # 图存储
  neo4j:
    image: neo4j:5
    environment:
      NEO4J_AUTH: neo4j/password
      NEO4J_PLUGINS: '["apoc"]'
    ports:
      - "7474:7474"
      - "7687:7687"
    volumes:
      - neo4j_data:/data

  # API 服务
  api:
    build: .
    ports:
      - "8001:8001"
    depends_on:
      - timescale
      - postgis
      - chroma
      - neo4j
    environment:
      TIMESCALE_DSN: postgresql://postgres:password@timescale:5432/temporal_db
      POSTGIS_DSN: postgresql://postgres:password@postgis:5432/spatial_db
      CHROMA_HOST: chroma:8000
      NEO4J_URI: bolt://neo4j:7687

volumes:
  timescale_data:
  postgis_data:
  chroma_data:
  neo4j_data:
```

---

## 八、总结

### 组件对照表

| 组件 | 数据库 | 核心能力 | 查询示例 |
|------|--------|---------|---------|
| **时序存储** | TimescaleDB | 时间范围、聚合、模式 | "去年6月的记忆" |
| **空间存储** | PostGIS | 范围、距离、层级 | "500米内的记忆" |
| **语义存储** | ChromaDB | 相似度、模糊匹配 | "和GraphQL相关" |
| **图存储** | Neo4j | 关系、因果链、网络 | "导致这个决策的事件" |

### 核心优势

1. **时空语义融合**：不是简单叠加，而是深度融合
2. **多维查询**：时间、空间、内容、关系 四维查询
3. **灵活组合**：支持多种查询策略（时间优先/空间优先/语义优先）
4. **因果追踪**：通过图结构追踪记忆间的因果关系
5. **混合架构**：每种数据用最适合的存储方案

### 适用场景

| 场景 | 核心价值 |
|------|---------|
| **个人生活记录** | 时间线可视化、地点轨迹 |
| **项目开发管理** | 版本历史、地点决策追踪 |
| **企业知识管理** | 跨地域协作、项目演进 |
| **AI Agent 记忆** | 时空调知的长期记忆 |

---

## 参考资料

- TimescaleDB: https://www.timescale.com/
- PostGIS: https://postgis.net/
- ChromaDB: https://www.trychroma.com/
- Neo4j: https://neo4j.com/
