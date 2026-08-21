"""predicate 字典编码 — 主文档 §11.5(predicate 字符串 → int code).

设计取舍(简化决策,P2):
- predicate_dict 字典表 + triples.predicate_code 冗余编码列(schema 011)
- **读取路径不变**: 所有查询仍按 predicate TEXT 列过滤/返回,
  不重写为 JOIN 字典表 — 改动面可控,不触碰 Layer 1 查询契约
- **写入路径渐进编码**: StorageEngine.write_triple 经 encode_predicate
  取/建 code 一并落库;历史数据由 schema 011 迁移一次性回填;
  绕过 StorageEngine 的裸 SQL 写入(如 ScopePromoter 提升副本)
  允许 predicate_code 为 NULL,由归档流程(archiver)归档时补齐
- 体积收益主要体现在归档表(triples_archive 以 code 存储)与后续
  按 code 的统计/压缩;热表因保留 TEXT 列(读取兼容)收益有限 —
  实测见 tests/benchmarks/storage_size.py

零依赖、确定性: code 按插入顺序单调递增,字典本身即反查表.
"""

from __future__ import annotations

import aiosqlite


async def encode_predicate(conn: aiosqlite.Connection, predicate: str) -> int:
    """取 predicate 的字典编码,不存在则插入新编码(get-or-create).

    Args:
        conn: aiosqlite 连接(事务由调用方管理,本函数不主动 commit)
        predicate: 谓词字符串(非空)

    Returns:
        predicate_dict.code(单调递增 int)

    Raises:
        ValueError: predicate 为空
    """
    if not predicate:
        raise ValueError("predicate 不能为空")
    await conn.execute(
        "INSERT OR IGNORE INTO predicate_dict(predicate) VALUES (?)",
        [predicate],
    )
    cursor = await conn.execute(
        "SELECT code FROM predicate_dict WHERE predicate = ?",
        [predicate],
    )
    row = await cursor.fetchone()
    if row is None:  # 理论上不可达(INSERT OR IGNORE 已保证存在)
        raise RuntimeError(f"predicate_dict 编码失败: {predicate!r}")
    return int(row["code"])


async def decode_predicate(conn: aiosqlite.Connection, code: int) -> str | None:
    """按编码反查谓词字符串,不存在返回 None."""
    cursor = await conn.execute(
        "SELECT predicate FROM predicate_dict WHERE code = ?",
        [code],
    )
    row = await cursor.fetchone()
    return str(row["predicate"]) if row is not None else None


__all__ = [
    "decode_predicate",
    "encode_predicate",
]
