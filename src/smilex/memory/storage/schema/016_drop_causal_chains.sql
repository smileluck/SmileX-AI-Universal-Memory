-- 016 — 删除 causal_chains 物化表(2026-09 死重量清理)
--
-- 该表由 causal 任务维护,但全库无消费方: 检索走 trace_causal_chain 的
-- 递归 CTE 直遍 triples.predecessor_id,MCP memory_graph_query(mode=causal)
-- 同样走 CTE;唯一"读"是 stats 行数计数。任务因 causal_inference 事件
-- 永不触发(无 emit 方)且无时间触发,实为死任务。检索能力不变。

DROP TABLE IF EXISTS causal_chains;
