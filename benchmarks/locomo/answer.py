"""LoCoMo 答案生成(与 longmemeval 共用逻辑) — recall() 构建上下文 + LLM 生成答案.

Abstention 语义(LongMemEval/LoCoMo 通用): 上下文不足以回答时应答
"I don't know."(对所有题型统一生效,由判卷模型按 gold 判分).
"""

from __future__ import annotations

import time

from openai import AsyncOpenAI
from smilex.middlewares import MemoryMiddleware, RecallRequest

from llm_client import answer_model, chat

ANSWER_SYSTEM = (
    "You are a helpful assistant with access to the user's long-term memory "
    "(retrieved conversation excerpts). Answer the question using ONLY the "
    "provided memory context. If the context does not contain the answer, "
    "reply exactly: I don't know. Be concise (one or two sentences)."
)


async def answer_question(
    mw: MemoryMiddleware,
    question: str,
    client: AsyncOpenAI,
    *,
    token_budget: int = 4000,
    top_k: int = 10,
) -> dict:
    """检索 + 生成. 返回 {answer, context_tokens, context, recall_s, llm_tokens}."""
    req = RecallRequest(
        query=question,
        top_k=top_k,
        token_budget=token_budget,
    )
    start = time.perf_counter()
    resp = await mw.recall(req, session_id=None)  # 不走 L0 通道,纯长期记忆
    recall_s = time.perf_counter() - start

    user_prompt = (
        f"### Memory context\n{resp.context or '(empty)'}\n\n### Question\n{question}"
    )
    answer_text, llm_tokens = await chat(
        # 保留思考模式(GLM 默认开启): 长上下文事实抽取显著更准,
        # max_tokens 给足以容纳 reasoning + 回答
        client, answer_model(), ANSWER_SYSTEM, user_prompt, max_tokens=2048
    )
    return {
        "answer": answer_text,
        "context_tokens": resp.token_count,
        "context": resp.context,
        "recall_s": recall_s,
        "llm_tokens": llm_tokens,
    }
