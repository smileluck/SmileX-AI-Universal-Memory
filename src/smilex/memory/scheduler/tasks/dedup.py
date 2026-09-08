"""近重复合并任务(§ 主动优化一期,参照 DSH 治理模式).

对同 scope 同 layer 的近重复 fragment 做确定性合并:
- 候选发现: 用自身规范化内容前 200 字作 FTS5 trigram 短语查询(同 scope/
  layer,排除自身),零依赖复用既有检索通道;trigram 短语对近同文召回率高,
  再以下面的精确度量把关
- 确认度量: 规范化(仅保留字母数字与 CJK)后取字符 bigram 集合,
  Jaccard ≥ threshold(默认 0.7,DSH 中文校准值)判定为近重复
- 合并语义: 幸存者 = created_at 较早者(稳定防抖动,id 平局裁决);吸收重复
  行的 access_count(求和)/last_accessed_at(取新)/时间区间(并集)/
  importance(取 max)/entities(JSON 并集);重复行连向量一起删除
  (vector_links 有 FK 引用热表,须先清 vector 再删行 — 同 Archiver);
  不改幸存者 content/embedding/updated_at(FTS 索引与衰减锚点不受扰)
- 断点: cursor = 已处理源行 id;本批内已合并集合即时生效,传递链
  (A~B~C)在后续扫描中自然收敛

payload:
    scope / batch_size / step_delay: 同整合任务
    layer: 参与去重的层(默认 "L1")
    threshold: Jaccard 阈值(默认 0.7)
    min_length: 规范化后低于此长度不参与(默认 12;短串 bigram 区分度差)
    max_candidates: 每行候选上限(默认 5,防长尾扫描)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from typing import TYPE_CHECKING, Any

from ...observability.logging import get_logger
from ._common import _scope_clause

if TYPE_CHECKING:
    from ...storage.storage_engine import StorageEngine
    from ..checkpoint import InterruptContext

_logger = get_logger("dedup")

# 规范化保留: 字母数字(小写化后)与 CJK 统一表意文字
_KEEP_RE = re.compile(r"[0-9a-z\u4e00-\u9fff]+")

_PHRASE_MAX_CHARS = 200


def _normalize(text: str) -> str:
    """小写化并只保留字母数字与 CJK 字符(标点/空白/符号不参与度量)."""
    return "".join(_KEEP_RE.findall(text.lower()))


def _bigrams(normalized: str) -> frozenset[str]:
    """字符 bigram 集合;单字符文本退化为该字符本身."""
    if len(normalized) < 2:
        return frozenset({normalized} if normalized else "")
    return frozenset(normalized[i : i + 2] for i in range(len(normalized) - 1))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """集合 Jaccard 相似度(空集对返回 0)."""
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _fts_phrase(text: str) -> str:
    """FTS5 短语转义: 双引号包裹,内部引号加倍(api._fts_phrase 同式)."""
    return '"' + text.replace('"', '""') + '"'


async def dedup(
    storage: StorageEngine, ctx: InterruptContext, payload: dict[str, Any]
) -> dict[str, Any]:
    """近重复合并 — 见模块 docstring;返回 scanned/merged/merged_pairs 统计."""
    conn = storage.conn
    scope = payload.get("scope")
    layer = payload.get("layer", "L1")
    threshold = float(payload.get("threshold", 0.7))
    min_length = int(payload.get("min_length", 12))
    max_candidates = int(payload.get("max_candidates", 5))
    batch_size = int(payload.get("batch_size", 50))
    step_delay = float(payload.get("step_delay", 0.0))

    where, params = _scope_clause(scope, "layer = ?", [layer])
    last_id = ctx.cursor if isinstance(ctx.cursor, str) else ""
    stats = {
        "scanned": 0,
        "merged": 0,
        "skipped_short": 0,
        "no_candidate": 0,
        **ctx.state.get("stats", {}),
    }
    step = ctx.step

    while True:
        cur = await conn.execute(
            "SELECT id, fragment_id, content, entities, importance, access_count, "
            "last_accessed_at, time_start, time_end, created_at, scope "
            f"FROM temporal_fragments WHERE {where} AND id > ? ORDER BY id LIMIT ?",
            [*params, last_id, batch_size],
        )
        rows = list(await cur.fetchall())
        if not rows:
            break
        merged_away: set[str] = set()
        for row in rows:
            stats["scanned"] += 1
            normalized = _normalize(str(row["content"]))
            if len(normalized) < min_length:
                stats["skipped_short"] += 1
                continue
            gram_set = _bigrams(normalized)
            candidates = await _find_candidates(
                conn, row, layer=layer, limit=max_candidates
            )
            merged_any = False
            for cand in candidates:
                if cand["id"] in merged_away:
                    continue
                if jaccard(gram_set, _bigrams(_normalize(str(cand["content"])))) < threshold:
                    continue
                survivor, duplicate = _pick_survivor(row, cand)
                await _absorb(conn, survivor, duplicate)
                merged_away.add(duplicate["id"])
                stats["merged"] += 1
                merged_any = True
                _logger.info(
                    "dedup_merged",
                    survivor=survivor["id"],
                    duplicate=duplicate["id"],
                )
            if not merged_any and not candidates:
                stats["no_candidate"] += 1
        await conn.commit()
        last_id = rows[-1]["id"]
        step += 1
        if step_delay > 0:
            await asyncio.sleep(step_delay)
        await ctx.checkpoint(
            step=step,
            state={"stats": stats},
            cursor=last_id,
        )
    return stats


async def _find_candidates(
    conn: Any,
    row: Any,
    *,
    layer: str,
    limit: int,
) -> list[Any]:
    """FTS trigram 短语查询找候选(同 scope/layer,排除自身).

    scope 取行自身的值(任务级 scope 只约束扫描范围,合并必须同 scope)。
    """
    phrase = _fts_phrase(str(row["content"])[:_PHRASE_MAX_CHARS])
    try:
        cur = await conn.execute(
            "SELECT tf.id, tf.content, tf.entities, tf.importance, tf.access_count, "
            "tf.last_accessed_at, tf.time_start, tf.time_end, tf.created_at "
            "FROM fts_fragments fts JOIN temporal_fragments tf ON tf.rowid = fts.rowid "
            "WHERE fts_fragments MATCH ? AND tf.layer = ? AND tf.scope = ? "
            "AND tf.id != ? ORDER BY tf.id LIMIT ?",
            [phrase, layer, row["scope"], row["id"], limit],
        )
        return list(await cur.fetchall())
    except Exception:  # noqa: BLE001 — FTS 不可用(旧库)时静默跳过该行
        return []


def _pick_survivor(a: Any, b: Any) -> tuple[Any, Any]:
    """幸存者 = created_at 较早者(相同则 id 较小),返回 (幸存, 重复)."""
    a_key = (str(a["created_at"]), str(a["id"]))
    b_key = (str(b["created_at"]), str(b["id"]))
    return (a, b) if a_key <= b_key else (b, a)


async def _absorb(conn: Any, survivor: Any, duplicate: Any) -> None:
    """重复行的访问/时间/实体并入幸存者,然后连同向量删除重复行."""
    entities = _merge_entities(survivor["entities"], duplicate["entities"])
    last_accessed = _max_str(
        survivor["last_accessed_at"], duplicate["last_accessed_at"]
    )
    time_start = _min_str(survivor["time_start"], duplicate["time_start"])
    time_end = _max_str(survivor["time_end"], duplicate["time_end"])
    await conn.execute(
        "UPDATE temporal_fragments SET entities = ?, importance = MAX(importance, ?), "
        "access_count = access_count + ?, last_accessed_at = ?, time_start = ?, "
        "time_end = ? WHERE id = ?",
        (
            entities,
            float(duplicate["importance"] or 0),
            int(duplicate["access_count"] or 0),
            last_accessed,
            time_start,
            time_end,
            survivor["id"],
        ),
    )
    # vector_links.fragment_id 有 FK 引用热表: 先清向量再删行(同 Archiver);
    # memory_vectors 虚拟表在 vec 扩展未加载的库里不存在,缺表时只清 links
    with contextlib.suppress(Exception):  # noqa: BLE001 — 无 vec 表(如测试库)时跳过
        await conn.execute(
            "DELETE FROM memory_vectors WHERE vector_id IN ("
            "SELECT vector_id FROM vector_links WHERE fragment_id = ?)",
            [duplicate["id"]],
        )
    await conn.execute(
        "DELETE FROM vector_links WHERE fragment_id = ?", [duplicate["id"]]
    )
    await conn.execute(
        "DELETE FROM temporal_fragments WHERE id = ?", [duplicate["id"]]
    )


def _merge_entities(left: Any, right: Any) -> str:
    """两组 entities JSON 串做保序并集后重新序列化."""
    try:
        merged = list(dict.fromkeys([*json.loads(left or "[]"), *json.loads(right or "[]")]))
    except (ValueError, TypeError):
        merged = []
    return json.dumps(merged, ensure_ascii=False)


def _min_str(a: Any, b: Any) -> Any:
    vals = [v for v in (a, b) if v]
    return min(vals) if vals else None


def _max_str(a: Any, b: Any) -> Any:
    vals = [v for v in (a, b) if v]
    return max(vals) if vals else None
