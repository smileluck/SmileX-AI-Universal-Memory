-- locations — 位置层级(替代 PostGIS LTREE)
-- 依据: 主文档 §6.1 字段摘要 + §6.5 空间层级查询 path LIKE 'home.%'
-- 索引: B-tree on path(替代 LTREE)

CREATE TABLE IF NOT EXISTS locations (
    id            TEXT    PRIMARY KEY,
    location_id   TEXT    NOT NULL UNIQUE,             -- 归一化 ID(如 "home.living_room")
    name          TEXT    NOT NULL,
    location_type TEXT    NOT NULL,                    -- point/area/region
    parent_id     TEXT,                                -- 父节点(层级关系)
    path          TEXT    NOT NULL,                    -- 路径,如 "home.living_room"
    coordinates   TEXT,                                -- JSON: {"lng":..., "lat":...}
    scope         TEXT    NOT NULL DEFAULT 'global',
    valid_from    TEXT    NOT NULL,
    valid_to      TEXT,

    CHECK (location_type IN ('point', 'area', 'region')),
    CHECK (scope = 'global' OR scope LIKE 'project:%' OR scope LIKE 'tenant:%'),
    FOREIGN KEY (parent_id) REFERENCES locations(id)
);

CREATE INDEX IF NOT EXISTS idx_locations_path       ON locations(path);
CREATE INDEX IF NOT EXISTS idx_locations_parent     ON locations(parent_id) WHERE parent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_locations_scope_path ON locations(scope, path);
CREATE INDEX IF NOT EXISTS idx_locations_valid      ON locations(valid_from, valid_to);
