-- project_current_state — 项目当前状态汇总(触发器维护)
-- 依据: 主文档 §6.3 完整 DDL + 触发器(直接引用,无修改)
-- 用途: SQLite 不支持物化视图,用触发器维护的汇总表等效实现
-- 关系: 主键 (scope, subject_id, predicate),关系型三元组的"最新版本"

CREATE TABLE IF NOT EXISTS project_current_state (
    scope        TEXT NOT NULL,
    subject_id   TEXT NOT NULL,
    predicate    TEXT NOT NULL,
    object_id    TEXT,
    object_value TEXT,
    valid_from   TEXT NOT NULL,
    confidence   REAL DEFAULT 1.0,
    PRIMARY KEY (scope, subject_id, predicate)
);

-- 触发器: 写入 triples 时,对 project_state/task_status/config 关系自动更新当前状态
-- 文档 §6.3 完整定义,这里一字不改
CREATE TRIGGER IF NOT EXISTS trg_update_current_state
AFTER INSERT ON triples
WHEN NEW.relation_type IN ('project_state', 'task_status', 'config')
   AND NEW.scope LIKE 'project:%'
BEGIN
    -- 1. 旧版本置为失效(valid_to 标记)
    UPDATE triples SET valid_to = NEW.valid_from
    WHERE subject_id = NEW.subject_id
      AND predicate = NEW.predicate
      AND scope = NEW.scope
      AND valid_to IS NULL
      AND id != NEW.id;

    -- 2. UPSERT 当前状态
    INSERT INTO project_current_state
        (scope, subject_id, predicate, object_id, object_value, valid_from, confidence)
    VALUES
        (NEW.scope, NEW.subject_id, NEW.predicate, NEW.object_id, NEW.object_value, NEW.valid_from, NEW.confidence)
    ON CONFLICT(scope, subject_id, predicate) DO UPDATE SET
        object_id = excluded.object_id,
        object_value = excluded.object_value,
        valid_from = excluded.valid_from,
        confidence = excluded.confidence;
END;
