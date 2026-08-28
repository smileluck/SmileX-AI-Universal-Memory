"""LLM-as-judge — 对比生成答案与 gold answer(mem0 同款做法).

判卷要求: 语义等价即正确(数字/单位/别名宽容);abstention 题的 gold
即 "I don't know.",拒答与 gold 一致才得分.
"""

from __future__ import annotations

import re

from openai import AsyncOpenAI

from llm_client import chat, judge_model

JUDGE_SYSTEM = (
    "You are a strict grader. Compare the predicted answer against the gold "
    "answer for the given question. The prediction is correct ONLY if it "
    "conveys the same fact(s) as the gold answer (tolerate formatting, "
    "units, and alias differences; missing key details = incorrect; "
    "'I don't know.' matches only when the gold answer says so). "
    "Reply with exactly one line: 'yes' or 'no'."
)

JUDGE_USER_TMPL = """### Question
{question}

### Gold answer
{gold}

### Predicted answer
{prediction}

Is the predicted answer correct? (yes/no)"""


def _parse_verdict(text: str) -> bool:
    m = re.search(r"\b(yes|no)\b", text.lower())
    if m is None:
        return False
    return m.group(1) == "yes"


async def judge_answer(
    client: AsyncOpenAI, question: str, gold: str, prediction: str
) -> tuple[bool, str, int]:
    """判卷. 返回 (是否正确, judge 原始输出, judge tokens)."""
    verdict_text, tokens = await chat(
        client,
        judge_model(),
        JUDGE_SYSTEM,
        JUDGE_USER_TMPL.format(question=question, gold=gold, prediction=prediction),
        max_tokens=32,
        disable_thinking=True,
    )
    return _parse_verdict(verdict_text), verdict_text, tokens
