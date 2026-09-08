"""错误指纹与教训闭环(§ 主动优化收官,DSH 自我纠错模式的确定性实现).

设计(对齐 dsh-agent-memory 的"错误指纹 → 第 2 次提示 → 教训固化"):
- agent 出错时调 MCP 工具 ``memory_report_error(code, message)`` 上报 —
  服务端做确定性指纹计数(零 LLM 零轮询)
- 同指纹第 2 次出现 → 响应置 ``should_write_lesson``,附教训模板;
  已有教训 → 响应直接携带教训内容(agent 立即得到纠偏)
- agent 经 ``memory_write(error_fingerprint=...)`` 沉淀教训: 工具层加
  【教训】前缀 + importance 提升至 0.95(自动落入删除守卫 ≥0.9 保护与
  访问续命),写入后回链 lesson_id
- lessonize 辩证规则以协议文本承载: 区分错误原因 / 失败条件与适用边界 /
  可保留部分,**禁止把条件性失败记成绝对结论**
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from ..memory.storage.storage_engine import scope_path
from ..utils.timeutil import now_utc, to_iso

# 教训内容前缀(与任务侧 【摘要】/【整合】 前缀约定同族;面板/检索可识别)
LESSON_PREFIX = "【教训】"
# 教训写入的最低重要度 — ≥ protect_importance(0.9),删除守卫自动保护
LESSON_IMPORTANCE = 0.95
# 同指纹第 N 次出现开始建议写教训(DSH 校准: 第 2 次)
LESSON_PROMPT_THRESHOLD = 2

_WHITESPACE_RE = re.compile(r"\s+")
_DIGITS_RE = re.compile(r"\d+")

_LESSON_TEMPLATE = (
    "排查后用 memory_write(error_fingerprint=<指纹>) 沉淀教训,内容按三段写:"
    "①错误原因(根因,非表象);②失败条件与适用边界(什么情况下会复现);"
    "③可保留做法(其余场景仍有效的部分)。禁止把条件性失败记成绝对结论。"
)


def normalize_error_text(text: str) -> str:
    """错误文本规范化: 小写 + 空白折叠 + 数字替换为 #(剥离路径/时间戳/ID
    等随机差异,让同一错误的不同实例归到同一指纹)."""
    normalized = _WHITESPACE_RE.sub(" ", (text or "").strip().lower())
    return _DIGITS_RE.sub("#", normalized)[:200]


def error_fingerprint(
    scope_str: str, code: str, message: str
) -> str:
    """指纹 = sha256(scope|code|规范化message)[:16];scope 折进指纹
    (同一错误在不同项目分别计数)."""
    raw = "|".join(
        [scope_str, normalize_error_text(code), normalize_error_text(message)]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


async def register_error(
    conn: Any,
    *,
    scope_str: str,
    code: str,
    message: str,
) -> dict[str, Any]:
    """登记一次错误并返回闭环状态(计数/是否建议写教训/已有教训 id).

    UPSERT 原子累加;响应组装(取教训内容/生成 advice)由调用方完成。
    """
    fp = error_fingerprint(scope_str, code, message)
    now = to_iso(now_utc())
    await conn.execute(
        "INSERT INTO error_fingerprints(fingerprint, count, first_seen, "
        "last_seen, sample_code, sample_message) "
        "VALUES (?, 1, ?, ?, ?, ?) "
        "ON CONFLICT(fingerprint) DO UPDATE SET "
        "count = count + 1, last_seen = excluded.last_seen",
        [fp, now, now, normalize_error_text(code), normalize_error_text(message)],
    )
    await conn.commit()
    cur = await conn.execute(
        "SELECT count, first_seen, last_seen, lesson_id FROM error_fingerprints "
        "WHERE fingerprint = ?",
        [fp],
    )
    row = await cur.fetchone()
    assert row is not None
    return {
        "fingerprint": fp,
        "count": int(row["count"]),
        "first_seen": str(row["first_seen"]),
        "last_seen": str(row["last_seen"]),
        "lesson_id": str(row["lesson_id"]) if row["lesson_id"] else None,
    }


async def load_lesson_content(conn: Any, lesson_id: str) -> str | None:
    """取关联教训 fragment 的内容(不存在/已归档返回 None)."""
    cur = await conn.execute(
        "SELECT content FROM temporal_fragments WHERE id = ?", [lesson_id]
    )
    row = await cur.fetchone()
    return str(row["content"]) if row is not None else None


async def link_lesson(conn: Any, fingerprint: str, lesson_id: str) -> None:
    """教训写入成功后回链(同指纹重写 → 指向最新,旧教训失链不删除)."""
    await conn.execute(
        "UPDATE error_fingerprints SET lesson_id = ? WHERE fingerprint = ?",
        [lesson_id, fingerprint],
    )
    await conn.commit()


def build_advice(status: dict[str, Any], lesson_content: str | None) -> dict[str, Any]:
    """组装 report_error 的响应载荷(advice 指令驱动 agent 固化/遵循教训)."""
    count = int(status["count"])
    payload: dict[str, Any] = {
        "fingerprint": status["fingerprint"],
        "count": count,
        "first_seen": status["first_seen"],
        "last_seen": status["last_seen"],
        "lesson": lesson_content,
        "should_write_lesson": False,
    }
    if lesson_content:
        payload["advice"] = (
            f"该错误已出现 {count} 次且已有教训,直接遵循上面的 lesson 内容处理;"
            "若教训不再适用,用 memory_write(error_fingerprint=...) 重写"
            "(新教训自动取代旧链)。"
        )
    elif count >= LESSON_PROMPT_THRESHOLD:
        payload["should_write_lesson"] = True
        payload["advice"] = (
            f"同一错误已出现 {count} 次仍未沉淀教训。{_LESSON_TEMPLATE}"
        )
    else:
        payload["advice"] = (
            "首次记录该错误。排查根因后,若得出可复用结论,"
            "调 memory_write(content=..., error_fingerprint="
            f"\"{status['fingerprint']}\") 沉淀为教训。"
        )
    return payload


def prepare_lesson_write(
    content: str, importance: float
) -> tuple[str, float]:
    """工具层教训写前处理: 【教训】前缀 + 重要度提升至守卫保护线."""
    text = content if content.startswith(LESSON_PREFIX) else f"{LESSON_PREFIX}{content}"
    return text, max(importance, LESSON_IMPORTANCE)


def resolve_scope_str(
    memory: Any, scope: str, scope_id: str | None
) -> str:
    """MCP 工具参数 → scope 全路径(scope_id 缺省回落当前项目,与 write 一致).

    PROJECT/TENANT 无可用 scope_id(尚未初始化项目)时回落 global —
    错误上报不应因缺项目上下文而失败。
    """
    from ..memory.models import MemoryScope

    enum_scope = MemoryScope.PROJECT if scope is None else MemoryScope(scope)
    effective_id = scope_id or getattr(memory, "current_scope_id", None)
    try:
        return scope_path(enum_scope, effective_id)
    except ValueError:
        return scope_path(MemoryScope.GLOBAL, None)
