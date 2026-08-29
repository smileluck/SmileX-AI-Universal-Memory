"""FTS5 BM25 关键词检索 — L1 双通道的关键词侧(schema 013_fts5).

- bm25_fragment_search(conn, query_text, scope_filter, top_k)
- trigram tokenizer: 中英文通吃(CJK 子串),ASCII 大小写不敏感
- 问句按空白分词,丢弃 <3 字符的词(trigram 最小长度),OR 连接
  (长问句 AND 语义过严,几乎匹配不到任何行)
- 与向量 KNN 的融合在 ContextBuilder(RRF),本模块只负责关键词排序
"""

from __future__ import annotations

import re

import aiosqlite

from ...models import ScopeFilter
from ._scope import build_scope_clause

# trigram tokenizer 的最小词长(<3 字符的词无法生成 trigram)
_MIN_TERM_CHARS = 3

# 截断超长问句,避免生成巨型 OR 查询
_MAX_TERMS = 16

_WORD_RE = re.compile(r"[\w]+")


def build_fts_query(query_text: str) -> str | None:
    """自然语言问句 → FTS5 MATCH 表达式(词 OR 连接).

    Returns:
        MATCH 表达式;无有效词(全 <3 字符)时返回 None(跳过关键词通道).
    """
    terms: list[str] = []
    for word in _WORD_RE.findall(query_text):
        if len(word) < _MIN_TERM_CHARS:
            continue
        # 引号转义,防 FTS 语法注入
        escaped = word.replace('"', '""')
        term = f'"{escaped}"'
        if term not in terms:
            terms.append(term)
        if len(terms) >= _MAX_TERMS:
            break
    if not terms:
        return None
    return " OR ".join(terms)


async def bm25_fragment_search(
    conn: aiosqlite.Connection,
    query_text: str,
    *,
    scope_filter: ScopeFilter | None = None,
    top_k: int = 20,
) -> list[str]:
    """BM25 关键词检索 temporal_fragments,返回按相关性的 fragment id 列表.

    Args:
        conn: aiosqlite 连接(schema ≥ 13,fts_fragments 虚拟表)
        query_text: 自然语言问句(分词见 build_fts_query)
        scope_filter: 作用域过滤(JOIN temporal_fragments.scope)
        top_k: 返回条数上限

    Returns:
        [fragment_id, ...] 按相关性降序;无有效词/无命中返回空列表.
    """
    match_expr = build_fts_query(query_text)
    if match_expr is None:
        return []

    scope_clause, scope_params = build_scope_clause(scope_filter)
    scope_where = f" AND ({scope_clause})" if scope_clause else ""

    cursor = await conn.execute(
        "SELECT f.id FROM fts_fragments "
        "JOIN temporal_fragments f ON f.rowid = fts_fragments.rowid "
        f"WHERE fts_fragments MATCH ?{scope_where} "
        "ORDER BY bm25(fts_fragments) LIMIT ?",
        [match_expr, *scope_params, top_k],
    )
    rows = await cursor.fetchall()
    return [str(r["id"]) for r in rows]


__all__ = ["bm25_fragment_search", "build_fts_query"]
