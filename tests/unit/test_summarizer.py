"""Unit tests for Summarizer 组件(P3 LLM 摘要压缩,§ 演进路径)."""

from __future__ import annotations

import pytest

from smilex.memory.summarizer import (
    LLMSummarizer,
    RuleSummarizer,
    SummarizerConfig,
    get_summarizer,
    rule_summary,
)

LONG = "第一句结论。" * 60  # 360 字,超过默认触发长度
SHORT = "短内容"


# ---------- 规则后端(与旧任务内联逻辑逐字一致) ----------

def test_rule_summary_matches_legacy_behavior():
    # ≤ summary_length 原样返回
    assert rule_summary("abc", 120) == "abc"
    # 超长: 截 120 字并在句末标点回退,附原文计数
    out = rule_summary(LONG, 120)
    assert out.startswith("第一句结论")
    assert out.endswith("…(原文共 360 字)")
    # 截断点必是句末标点
    head = out[: out.rfind("…")]
    assert head.endswith(("。", "!", "?"))


def test_rule_summarizer_async_and_config():
    s = RuleSummarizer(10)
    result = await_(s.summarize("a" * 30))
    assert result.endswith("…(原文共 30 字)")
    with pytest.raises(ValueError, match="正整数"):
        RuleSummarizer(0)


def await_(coro):
    import asyncio

    return asyncio.run(coro)


# ---------- 工厂 ----------

def test_factory_backends():
    assert isinstance(get_summarizer(), RuleSummarizer)
    assert isinstance(
        get_summarizer(SummarizerConfig(backend="rule")), RuleSummarizer
    )
    llm = get_summarizer(SummarizerConfig(backend="llm", model="test-m"))
    assert isinstance(llm, LLMSummarizer)
    with pytest.raises(ValueError, match="summarizer backend"):
        get_summarizer(SummarizerConfig(backend="nlp"))


# ---------- LLM 后端(fake client,不依赖真实 key) ----------

class _FakeCompletions:
    def __init__(self, text: str = "LLM 摘要正文", *, error: Exception | None = None):
        self._text = text
        self._error = error
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        message = type("M", (), {"content": self._text})()
        choice = type("C", (), {"message": message})()
        return type("R", (), {"choices": [choice]})()


class _FakeClient:
    def __init__(self, completions: _FakeCompletions):
        self.chat = type("Chat", (), {"completions": completions})()


def _patch_client(monkeypatch, completions: _FakeCompletions) -> None:
    monkeypatch.setattr(
        LLMSummarizer, "_load_client", lambda self: _FakeClient(completions)
    )


def test_llm_backend_happy_path(monkeypatch):
    comp = _FakeCompletions("这是 LLM 摘要")
    _patch_client(monkeypatch, comp)
    s = LLMSummarizer(api_key="k", model="gpt-test")
    out = await_(s.summarize(LONG))
    assert out == "这是 LLM 摘要"
    # 请求形状: temperature 0 + system/user 双消息
    assert comp.calls[0]["temperature"] == 0.0
    assert comp.calls[0]["model"] == "gpt-test"
    assert comp.calls[0]["messages"][0]["role"] == "system"
    assert "extra_body" not in comp.calls[0]  # 非 GLM 模型不带 thinking 开关


def test_llm_glm_disables_thinking(monkeypatch):
    comp = _FakeCompletions("glm 摘要")
    _patch_client(monkeypatch, comp)
    s = LLMSummarizer(api_key="k", model="GLM-4.5-Flash")
    await_(s.summarize(LONG))
    assert comp.calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}


def test_llm_failure_degrades_to_rule(monkeypatch):
    comp = _FakeCompletions(error=RuntimeError("api down"))
    _patch_client(monkeypatch, comp)
    s = LLMSummarizer(api_key="k", summary_length=100)
    out = await_(s.summarize(LONG))
    assert out.endswith("…(原文共 360 字)")  # 规则回退,不抛


def test_llm_empty_output_degrades(monkeypatch):
    comp = _FakeCompletions("   ")
    _patch_client(monkeypatch, comp)
    s = LLMSummarizer(api_key="k")
    out = await_(s.summarize(LONG))
    assert out.endswith("…(原文共 360 字)")


def test_llm_no_api_key_permanent_degrade():
    s = LLMSummarizer(api_key=None)
    assert s._load_client() is None
    out = await_(s.summarize(LONG))
    assert out.endswith("…(原文共 360 字)")


def test_env_resolution(monkeypatch):
    monkeypatch.setenv("SMILEX_SUMMARIZE_API_KEY", "env-key")
    monkeypatch.setenv("SMILEX_SUMMARIZE_MODEL", "env-model")
    monkeypatch.setenv("SMILEX_SUMMARIZE_BASE_URL", "http://env-gateway/v1")
    s = LLMSummarizer()
    assert s._api_key == "env-key"
    assert s._model == "env-model"
    assert s._base_url == "http://env-gateway/v1"
    # 构造参数优先于环境变量
    s2 = LLMSummarizer(model="param-model", api_key="param-key")
    assert s2._model == "param-model" and s2._api_key == "param-key"
