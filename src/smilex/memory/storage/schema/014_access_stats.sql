-- 014: 检索反馈闭环(§ 主动优化一期)
-- access_count/last_accessed_at 由 recall 命中时累加(track_access 开关,默认开);
-- forget 任务的存活分引入"使用即续命(last_accessed_at 重置衰减锚点)+
-- 常用即升值(log10 访问加成,上限 3x)"。FTS 触发器只在 UPDATE OF content
-- 联动,计数更新不触碰索引。
ALTER TABLE temporal_fragments ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE temporal_fragments ADD COLUMN last_accessed_at TEXT;
