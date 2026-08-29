"""OpenAI 兼容 API 客户端 — benchmark 的问答与判卷模型接入.

配置(环境变量):
- OPENAI_API_KEY            必填
- OPENAI_BASE_URL           可选(兼容代理/私有网关)
- SMILEX_BENCH_ANSWER_MODEL 答案生成模型(默认 gpt-4o-mini)
- SMILEX_BENCH_JUDGE_MODEL  判卷模型(默认 gpt-4o-mini)

GLM 说明: glm-4.5 系列默认开启思考模式,小 max_tokens 会让 content 为空
(reasoning_content 耗尽预算),本客户端对 glm-* 模型自动附加
thinking disabled;其他 OpenAI 兼容网关不受影响.
"""

from __future__ import annotations

import os
import sys

from openai import AsyncOpenAI

DEFAULT_MODEL = "gpt-4o-mini"


def answer_model() -> str:
    return os.environ.get("SMILEX_BENCH_ANSWER_MODEL", DEFAULT_MODEL)


def judge_model() -> str:
    return os.environ.get("SMILEX_BENCH_JUDGE_MODEL", DEFAULT_MODEL)


def _extra_body(model: str) -> dict | None:
    """GLM 系列: 关闭思考模式(判卷/短答案场景不需要,且避免 content 为空)."""
    if model.lower().startswith("glm"):
        return {"thinking": {"type": "disabled"}}
    return None


def get_client() -> AsyncOpenAI:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit(
            "缺少 OPENAI_API_KEY(LongMemEval 问答/判卷需要 LLM);"
            "可选 OPENAI_BASE_URL / SMILEX_BENCH_ANSWER_MODEL / SMILEX_BENCH_JUDGE_MODEL"
        )
    base_url = os.environ.get("OPENAI_BASE_URL") or None
    return AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=3)


async def chat(
    client: AsyncOpenAI, model: str, system: str, user: str, *,
    max_tokens: int = 512, disable_thinking: bool = False
) -> tuple[str, int]:
    """单轮对话;temperature 固定 0 保证可复现. 返回 (回复文本, total_tokens).

    disable_thinking: True 时对 glm-* 关闭思考模式(判卷等短输出场景,
    避免 reasoning_content 耗尽 max_tokens 导致 content 为空).
    生成场景保持模型默认(思考有助于长上下文事实抽取),max_tokens 需给足.
    """
    extra = _extra_body(model) if disable_thinking else None
    try:
        resp = await client.chat.completions.create(
            model=model,
            temperature=0.0,
            max_tokens=max_tokens,
            extra_body=extra,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
    except Exception as exc:
        # 内容安全过滤(智谱 1301 等 400 不可重试) / 瞬时网络异常重试耗尽等:
        # 打印后返回空文本,调用方按"生成失败"记 0 分,不让单题崩掉整个 run
        print(f"[llm] {type(exc).__name__}: {str(exc)[:120]}", file=sys.stderr)
        return "", 0
    content = resp.choices[0].message.content
    usage = resp.usage.total_tokens if resp.usage else 0
    return (content or "").strip(), usage
