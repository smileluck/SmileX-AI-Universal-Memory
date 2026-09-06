"""PII 脱敏 — PIIMasker Protocol + noop 默认 + 正则后端(§15.4 安全优化).

复刻 reranker/extractor 的可选组件模式:
- NoopPIIMasker(默认,零依赖): 直通 — agent 记忆常常就是要记住用户的
  联系方式/姓名,默认脱敏会损害记忆可用性,故默认关闭、config 一键开启
- RegexPIIMasker: 高置信度内置模式(带校验位,宁缺勿滥 — 版本号/日期
  不误伤),每类可独立选策略:
    - redact(默认): 整体替换为 ``[EMAIL]`` 占位符
    - hash: ``[EMAIL:9f86d081]`` — sha256 前 8 位,等值保持可连接
      (同一邮箱两次写入仍可关联),不可逆推原文
    - mask: 部分保留(``138****5678`` / ``j***@gmail.com``),人可辨识
      格式但不可复原完整值

内置类别与校验:
- email: RFC 近似格式
- phone: 中国大陆手机号 1[3-9]xxxxxxxxx
- id_card: 18 位身份证,GB11643 校验位验证(拒绝随机 18 位数)
- bank_card: 16-19 位,Luhn 校验(拒绝随机数字串)
- ipv4: 四段 IPv4,每段 ≤255(拒绝四段版本号形如 1.2.3.4? 注意: 版本
  号通常 ≤3 段,四段且全 ≤255 的版本号会被命中,默认策略下建议按需关闭)
- api_key: sk-/sk-ant-/AKIA/gh[pousr]_/Bearer/JWT 形态的高熵令牌

插入点: MemoryMiddleware.write() 入口(content/entities 名/
TripleInput.object_value/subject_name 统一脱敏后进入抽取与持久化);
扫描导入(git/markdown)路径不经过 write,不受影响(如需可后续扩展)。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

PII_BACKENDS = ("noop", "regex")
PII_CATEGORIES = ("api_key", "email", "id_card", "bank_card", "phone", "ipv4")
PII_STRATEGIES = ("redact", "hash", "mask")

_StrategyFn = Callable[[str], str]
_ValidatorFn = Callable[[str], bool]


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _id_card_checksum_valid(digits: str) -> bool:
    """GB11643 校验位: 前 17 位加权求和模 11 映射校验码."""
    if len(digits) != 18:
        return False
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    check_codes = "10X98765432"
    try:
        total = sum(
        int(d) * w for d, w in zip(digits[:17], weights, strict=True)
    )
    except ValueError:
        return False
    return check_codes[total % 11] == digits[17].upper()


def _luhn_valid(digits: str) -> bool:
    """Luhn 算法(银行卡校验)."""
    if not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _ipv4_valid(candidate: str) -> bool:
    parts = candidate.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def _default_mask(value: str) -> str:
    """自定义类别的默认部分保留策略(整串遮挡)."""
    return "***"


def _mask_email(value: str) -> str:
    local, _, domain = value.partition("@")
    head = local[:1] if local else ""
    return f"{head}***@{domain}" if domain else f"{head}***"


def _mask_digits(value: str, keep_head: int, keep_tail: int) -> str:
    """数字类部分保留: 头 keep_head + 尾 keep_tail,中间以 * 补齐原长."""
    if len(value) <= keep_head + keep_tail:
        return "*" * len(value)
    stars = "*" * (len(value) - keep_head - keep_tail)
    return value[:keep_head] + stars + value[-keep_tail:]


def _mask_ipv4(value: str) -> str:
    parts = value.split(".")
    return f"{parts[0]}.*.*.*"


def _mask_api_key(value: str) -> str:
    prefix = value.split("-", 1)[0] if "-" in value[:10] else value[:2]
    return f"{prefix}-***"


# 类别 → (正则, 可选校验器, mask 策略的部分保留函数)
_BUILTIN_PATTERNS: dict[str, tuple[re.Pattern[str], _ValidatorFn | None, _StrategyFn]] = {
    "api_key": (
        re.compile(
            r"(?:sk-ant-|sk-proj-|sk-)[A-Za-z0-9_-]{16,}"
            r"|AKIA[0-9A-Z]{16}"
            r"|gh[pousr]_[A-Za-z0-9]{30,}"
            r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
            r"|Bearer\s+[A-Za-z0-9._-]{20,}"
        ),
        None,
        _mask_api_key,
    ),
    "email": (
        re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
        None,
        _mask_email,
    ),
    # id_card 必须先于 bank_card: 18 位带 X 校验位的串两者正则都能命中
    "id_card": (
        re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
        _id_card_checksum_valid,
        lambda v: _mask_digits(v, 4, 2),
    ),
    "bank_card": (
        re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
        _luhn_valid,
        lambda v: _mask_digits(v, 4, 4),
    ),
    "phone": (
        re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
        None,
        lambda v: _mask_digits(v, 3, 4),
    ),
    "ipv4": (
        re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)"),
        _ipv4_valid,
        _mask_ipv4,
    ),
}


@runtime_checkable
class PIIMasker(Protocol):
    """写入前脱敏协议: 同步纯函数,幂等(脱敏后的文本再脱敏不变)."""

    def mask(self, text: str) -> str:
        """替换文本中的敏感值为占位符/哈希/部分保留形式."""
        ...


class NoopPIIMasker:
    """默认实现 — 直通(记忆系统的存在意义常包括记住用户联系方式)."""

    def mask(self, text: str) -> str:
        return text


class RegexPIIMasker:
    """正则脱敏后端 — 高置信度内置模式 + 自定义扩展.

    Args:
        strategies: 类别 → redact/hash/mask(未指定的类别用 redact;
            自定义 extra_patterns 的类别同理)
        extra_patterns: 类别名 → 正则字符串,与内置类别同等参与脱敏
            (策略/优先级按同一规则)
        disabled: 跳过的类别名(如 ipv4 误伤四段版本号时关闭)

    幂等性: 替换产物(方括号 + 类别名 + hex)不再匹配任何模式,
    重复 mask 结果不变。
    """

    def __init__(
        self,
        *,
        strategies: Mapping[str, str] | None = None,
        extra_patterns: Mapping[str, str] | None = None,
        disabled: tuple[str, ...] = (),
    ) -> None:
        merged: dict[str, tuple[re.Pattern[str], _ValidatorFn | None, _StrategyFn]] = {}
        for category, (pattern, validator, mask_fn) in _BUILTIN_PATTERNS.items():
            merged[category] = (pattern, validator, mask_fn)
        for category, regex in (extra_patterns or {}).items():
            merged[category] = (re.compile(regex), None, _default_mask)
        self._patterns = {
            cat: spec for cat, spec in merged.items() if cat not in set(disabled)
        }
        self._strategies: dict[str, str] = {}
        for category, strategy in (strategies or {}).items():
            if strategy not in PII_STRATEGIES:
                raise ValueError(
                    f"类别 {category!r} 的未知策略 {strategy!r},"
                    f"可选: {PII_STRATEGIES}"
                )
            self._strategies[category] = strategy
        unknown = [
            c for c in self._strategies if c not in merged and c not in set(disabled)
        ]
        if unknown:
            raise ValueError(
                f"未知 PII 类别: {unknown},可用: {sorted(merged)}"
            )

    def _replacer(
        self, category: str, mask_fn: _StrategyFn
    ) -> Callable[[re.Match[str]], str]:
        strategy = self._strategies.get(category, "redact")

        def replace(match: re.Match[str]) -> str:
            value = match.group(0)
            if strategy == "hash":
                return f"[{category.upper()}:{_hash_token(value)}]"
            if strategy == "mask":
                return mask_fn(value)
            return f"[{category.upper()}]"

        return replace

    def mask(self, text: str) -> str:
        result = text
        # 按声明顺序(api_key → email → id_card → bank_card → phone → ipv4):
        # 高特异性在前,避免数字类互相误吞;替换产物(方括号+类别名+hex)
        # 不再匹配任何模式,重复 mask 幂等
        for category, (pattern, validator, mask_fn) in self._patterns.items():
            replacer = self._replacer(category, mask_fn)
            if validator is None:
                result = pattern.sub(replacer, result)
            else:
                def checked(
                    m: re.Match[str],
                    v: _ValidatorFn = validator,
                    r: Callable[[re.Match[str]], str] = replacer,
                ) -> str:
                    return r(m) if v(m.group(0)) else m.group(0)

                result = pattern.sub(checked, result)
        return result


@dataclass(frozen=True)
class PIIConfig:
    """PII 脱敏工厂配置(对齐 ExtractorConfig 模式).

    Attributes:
        backend: "noop"(默认,直通)或 "regex"(高置信度正则脱敏)
        strategies: 类别 → redact/hash/mask(默认全 redact);
            如 {"email": "hash", "phone": "mask"}
        extra_patterns: 自定义类别 → 正则(策略同内置类别)
        disabled: 关闭的内置类别(如 ("ipv4",) 避免四段版本号误伤)
    """

    backend: str = "noop"
    strategies: Mapping[str, str] = field(default_factory=dict)
    extra_patterns: Mapping[str, str] = field(default_factory=dict)
    disabled: tuple[str, ...] = ()


def get_pii_masker(config: PIIConfig | None = None) -> PIIMasker:
    """工厂: 按 config.backend 返回 PIIMasker 实例(默认 NoopPIIMasker)."""
    config = config or PIIConfig()
    if config.backend == "noop":
        return NoopPIIMasker()
    if config.backend == "regex":
        return RegexPIIMasker(
            strategies=config.strategies,
            extra_patterns=config.extra_patterns,
            disabled=config.disabled,
        )
    raise ValueError(f"未知 pii_masker backend {config.backend!r},可选: {PII_BACKENDS}")
