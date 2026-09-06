"""Unit tests for PII 脱敏组件(§15.4)."""

from __future__ import annotations

import pytest

from smilex.memory.pii import (
    NoopPIIMasker,
    PIIConfig,
    RegexPIIMasker,
    get_pii_masker,
)


def test_noop_passthrough():
    text = "联系 alice@example.com / 13812345678"
    assert NoopPIIMasker().mask(text) == text
    assert get_pii_masker().mask(text) == text
    assert get_pii_masker(PIIConfig()).mask(text) == text


def test_email_redact_default():
    m = RegexPIIMasker()
    out = m.mask("发到 bob.smith@sub.example.org 谢谢")
    assert "bob.smith@sub.example.org" not in out
    assert "[EMAIL]" in out


def test_phone_mask_strategy():
    m = RegexPIIMasker(strategies={"phone": "mask"})
    out = m.mask("电话 13812345678")
    assert out == "电话 138****5678"


def test_hash_strategy_equality_preserving():
    m = RegexPIIMasker(strategies={"email": "hash"})
    a = m.mask("邮箱 same@x.io 哦")
    b = m.mask("又说 same@x.io 呢")
    other = m.mask("别的 diff@x.io 呀")
    token_a = a.split()[1]
    token_b = b.split()[1]
    assert token_a == token_b  # 同一邮箱 → 同一哈希(可连接)
    assert token_a != other.split()[1]
    assert token_a.startswith("[EMAIL:") and token_a.endswith("]")


def test_id_card_checksum_validated():
    m = RegexPIIMasker()
    # 合法校验位的 18 位身份证(GB11643): 11010519491231002X 为公开测试值
    out = m.mask("身份证 11010519491231002X")
    assert "[ID_CARD]" in out
    # 校验位不合法的 18 位数字串不应被身份证类别吞掉(也不满足 Luhn → 保留)
    out2 = m.mask("序列 12345678901234567")
    assert "12345678901234567" in out2


def test_bank_card_luhn_validated():
    m = RegexPIIMasker()
    # Luhn 合法的 16 位卡号(公开测试值 4111111111111111)
    out = m.mask("卡号 4111111111111111")
    assert "[BANK_CARD]" in out
    # Luhn 不合法的 16 位数字串不被替换
    out2 = m.mask("单号 4111111111111112")
    assert "4111111111111112" in out2


def test_ipv4_and_version_numbers():
    m = RegexPIIMasker()
    assert "[IPV4]" in m.mask("节点 192.168.1.10 挂了")
    # 常见三段版本号不误伤
    assert "v3.11.0" in m.mask("Python v3.11.0 发布")


def test_api_key_patterns():
    m = RegexPIIMasker()
    cases = [
        "sk-proj-AbCdEf123456789012345678",
        "sk-ant-api03AbCdEf12345678901234",
        "AKIAIOSFODNN7EXAMPLE",
        "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz1234567890",
        "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.sig",
    ]
    for case in cases:
        assert case not in m.mask(f"key={case}"), case


def test_mask_idempotent():
    m = RegexPIIMasker(strategies={"email": "hash", "phone": "mask"})
    once = m.mask("联系 a@b.io 或 13812345678")
    twice = m.mask(once)
    assert once == twice


def test_disabled_category():
    m = RegexPIIMasker(disabled=("ipv4",))
    assert "10.0.0.1" in m.mask("网关 10.0.0.1")


def test_extra_patterns():
    m = RegexPIIMasker(extra_patterns={"order_no": r"SO-\d{8}"})
    out = m.mask("订单 SO-20260905")
    assert "SO-20260905" not in out
    assert "[ORDER_NO]" in out


def test_unknown_strategy_and_category_rejected():
    with pytest.raises(ValueError, match="未知策略"):
        RegexPIIMasker(strategies={"email": "obfuscate"})
    with pytest.raises(ValueError, match="未知 PII 类别"):
        RegexPIIMasker(strategies={"passport": "hash"})


def test_factory_unknown_backend():
    with pytest.raises(ValueError, match="pii_masker backend"):
        get_pii_masker(PIIConfig(backend="nlp"))


def test_write_path_masks_relations_and_entities(tmp_path):
    """端到端: MemoryMiddleware(pii_masker=regex) 写入后 L1 内容已脱敏."""
    import asyncio

    from smilex.memory.contracts import TripleInput, WriteRequest
    from smilex.memory.models import MemoryScope
    from smilex.middlewares.memory import MemoryMiddleware

    async def run():
        db = tmp_path / "pii.db"
        mw = MemoryMiddleware(
            db,
            promotion_threshold=0,  # 全部直入 L1,便于查库断言
            pii_masker=get_pii_masker(PIIConfig(backend="regex")),
        )
        await mw.initialize()
        resp = await mw.write(
            WriteRequest(
                scope=MemoryScope.GLOBAL,
                content="客户 alice@example.com 下单",
                entities=["alice@example.com"],
                relations=[
                    TripleInput(
                        subject_name="alice@example.com",
                        predicate="email",
                        object_value="alice@example.com",
                    )
                ],
            ),
            session_id="s1",
        )
        assert resp.status.value == "saved"
        cur = await mw.engine.conn.execute(
            "SELECT content FROM temporal_fragments WHERE id = ?", [resp.memory_id]
        )
        row = await cur.fetchone()
        assert "alice@example.com" not in row["content"]
        assert "[EMAIL" in row["content"]
        cur = await mw.engine.conn.execute(
            "SELECT object_value FROM triples WHERE subject_id LIKE '%' LIMIT 1"
        )
        triple = await cur.fetchone()
        assert "alice@example.com" not in (triple["object_value"] or "")
        await mw.close()

    asyncio.run(run())
