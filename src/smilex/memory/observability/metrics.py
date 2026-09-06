"""零依赖指标注册表 — Prometheus 文本暴露格式(observability 子系统).

设计(对齐 §15.3 指标埋点,与 daemon 全 stdlib 的取向一致,不引入
prometheus-client):
- Counter / Gauge / Histogram 三类指标,label 维度受限枚举(调用方约定)
- Gauge 支持 callback 构造: render 时惰性取值(队列深度/库体积等
  "scrape 时才读" 的状态,写入路径零开销)
- render_prometheus() 输出 text/plain; version=0.0.4 文本格式,
  可被 Prometheus / VictoriaMetrics 直接抓取

线程模型: 与宿主一致的单事件循环访问(dict 读写无竞争);callback
也在 render(同循环)中求值。跨线程抓取需宿主自行序列化。

用法::

    registry = MetricsRegistry()
    writes = registry.counter("smilex_memory_write_total", "...", ("status",))
    writes.inc(status="saved")
    queue = registry.gauge("smilex_task_queue_depth", "...", callback=lambda: 3)
    latency = registry.histogram("smilex_memory_write_latency_seconds", "...")
    latency.observe(0.012)
    text = registry.render_prometheus()
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

# 延迟观测的默认桶(秒): 覆盖写入 ~2ms / 检索 ~6ms 到冷启动嵌入加载 >2.5s
DEFAULT_LATENCY_BUCKETS: tuple[float, ...] = (
    0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)

LabelValue = str | int | float
LabelValues = tuple[str, ...]
MetricValue = int | float


def _fmt_float(value: float) -> str:
    """指标值格式化: 整数值去小数点,其余保留足够精度."""
    if value == int(value):
        return str(int(value))
    return repr(round(value, 9))


def _escape_label(value: str) -> str:
    """label 值转义(Prometheus 文本格式: 反斜杠/引号/换行)."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _render_labels(label_names: Sequence[str], values: LabelValues) -> str:
    if not label_names:
        return ""
    pairs = ",".join(
        f'{name}="{_escape_label(v)}"'
        for name, v in zip(label_names, values, strict=True)
    )
    return "{" + pairs + "}"


class _Metric:
    """指标公共基类: 名称/帮助/label 名与子序列注册表."""

    def __init__(
        self,
        mtype: str,
        name: str,
        help_text: str,
        label_names: Sequence[str] = (),
    ) -> None:
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name):
            raise ValueError(f"指标名不合法: {name!r}")
        if len(set(label_names)) != len(label_names):
            raise ValueError(f"指标 {name} label 名重复: {label_names}")
        self.mtype = mtype
        self.name = name
        self.help_text = help_text
        self.label_names = tuple(label_names)
        self._children: dict[LabelValues, float] = {}

    def _key(self, labels: dict[str, LabelValue]) -> LabelValues:
        """kwargs → 按声明顺序排列的 label 值元组(缺键/未知键给清晰错误)."""
        unknown = set(labels) - set(self.label_names)
        if unknown:
            raise ValueError(
                f"指标 {self.name} 未声明的 label: {sorted(unknown)},"
                f"可用: {list(self.label_names)}"
            )
        missing = [n for n in self.label_names if n not in labels]
        if missing:
            raise ValueError(f"指标 {self.name} 缺少 label: {missing}")
        return tuple(str(labels[n]) for n in self.label_names)

    def _render_header(self) -> str:
        escaped = self.help_text.replace("\\", "\\\\").replace("\n", "\\n")
        return f"# HELP {self.name} {escaped}\n# TYPE {self.name} {self.mtype}\n"


class Counter(_Metric):
    """单调递增计数器."""

    def __init__(
        self,
        name: str,
        help_text: str,
        label_names: Sequence[str] = (),
    ) -> None:
        super().__init__("counter", name, help_text, label_names)

    def inc(self, amount: float = 1.0, **labels: LabelValue) -> None:
        if amount < 0:
            raise ValueError(f"Counter 只能递增,得到 {amount!r}")
        key = self._key(labels)
        self._children[key] = self._children.get(key, 0.0) + amount

    def value(self, **labels: LabelValue) -> float:
        """读子序列当前值(测试/健康检查用;不存在返回 0)."""
        return self._children.get(self._key(labels), 0.0)


class Gauge(_Metric):
    """瞬时值;callback 构造时值在 render 时惰性求值."""

    def __init__(
        self,
        name: str,
        help_text: str,
        label_names: Sequence[str] = (),
        *,
        callback: Callable[[], MetricValue] | None = None,
    ) -> None:
        super().__init__("gauge", name, help_text, label_names)
        if callback is not None and label_names:
            raise ValueError("callback gauge 不支持 label(单序列才有惰性语义)")
        self._callback = callback

    def set(self, value: float, **labels: LabelValue) -> None:
        if self._callback is not None:
            raise ValueError("callback gauge 由 render 求值,不可 set")
        self._children[self._key(labels)] = value

    def value(self, **labels: LabelValue) -> float:
        """读子序列当前值(callback gauge 求值 callback)."""
        if self._callback is not None:
            return float(self._callback())
        return self._children.get(self._key(labels), 0.0)


class _HistogramChild:
    """单 label 组合的桶累积状态(sum/count 桶间不共享)."""

    __slots__ = ("buckets", "counts", "sum", "count")

    def __init__(self, buckets: tuple[float, ...]) -> None:
        self.buckets = buckets
        self.counts = [0] * len(buckets)
        self.sum = 0.0
        self.count = 0


class Histogram(_Metric):
    """延迟/尺寸分布观测: 固定桶 + sum + count(render 为累计桶)."""

    def __init__(
        self,
        name: str,
        help_text: str,
        buckets: Sequence[float] | None = None,
        label_names: Sequence[str] = (),
    ) -> None:
        super().__init__("histogram", name, help_text, label_names)
        bucket_tuple = tuple(buckets) if buckets is not None else DEFAULT_LATENCY_BUCKETS
        if not bucket_tuple or list(bucket_tuple) != sorted(set(bucket_tuple)):
            raise ValueError(f"histogram {name} 桶必须非空且严格递增")
        self._bucket_tuple = bucket_tuple
        self._series: dict[LabelValues, _HistogramChild] = {}

    def observe(self, value: float, **labels: LabelValue) -> None:
        key = self._key(labels)
        child = self._series.get(key)
        if child is None:
            child = self._series[key] = _HistogramChild(self._bucket_tuple)
        child.sum += value
        child.count += 1
        # 只记入首个命中的桶(区段计数);render 时再转累计,
        # 两处都累计会重复累加
        for i, bound in enumerate(self._bucket_tuple):
            if value <= bound:
                child.counts[i] += 1
                break

    def _render(self) -> str:
        lines = [self._render_header()]
        for key, child in self._series.items():
            labels = _render_labels(self.label_names, key)
            cumulative = 0
            for bound, cnt in zip(self._bucket_tuple, child.counts, strict=True):
                cumulative += cnt
                le = f'le="{_fmt_float(bound)}"'
                bucket_labels = (
                    f"{labels[:-1]},{le}}}" if labels else "{" + le + "}"
                )
                lines.append(f"{self.name}_bucket{bucket_labels} {cumulative}\n")
            lines.append(f"{self.name}_sum{labels} {_fmt_float(child.sum)}\n")
            lines.append(f"{self.name}_count{labels} {child.count}\n")
        return "".join(lines)


class MetricsRegistry:
    """指标注册表: 同名重复注册返回既有实例(装配器可重入)."""

    def __init__(self) -> None:
        self._metrics: dict[str, _Metric] = {}

    def counter(
        self, name: str, help_text: str, label_names: Sequence[str] = ()
    ) -> Counter:
        metric = self._metrics.get(name)
        if metric is None:
            metric = Counter(name, help_text, label_names)
            self._metrics[name] = metric
        elif not isinstance(metric, Counter):
            raise ValueError(f"指标 {name} 已注册为 {type(metric).__name__}")
        return metric

    def gauge(
        self,
        name: str,
        help_text: str,
        label_names: Sequence[str] = (),
        *,
        callback: Callable[[], MetricValue] | None = None,
    ) -> Gauge:
        metric = self._metrics.get(name)
        if metric is None:
            metric = Gauge(name, help_text, label_names, callback=callback)
            self._metrics[name] = metric
        elif not isinstance(metric, Gauge):
            raise ValueError(f"指标 {name} 已注册为 {type(metric).__name__}")
        return metric

    def histogram(
        self,
        name: str,
        help_text: str,
        buckets: Sequence[float] | None = None,
        label_names: Sequence[str] = (),
    ) -> Histogram:
        metric = self._metrics.get(name)
        if metric is None:
            metric = Histogram(name, help_text, buckets, label_names)
            self._metrics[name] = metric
        elif not isinstance(metric, Histogram):
            raise ValueError(f"指标 {name} 已注册为 {type(metric).__name__}")
        return metric

    def render_prometheus(self) -> str:
        """渲染全部指标为 Prometheus 文本暴露格式(按注册顺序)."""
        parts: list[str] = []
        for metric in self._metrics.values():
            if isinstance(metric, Histogram):
                parts.append(metric._render())
                continue
            lines = [metric._render_header()]
            if isinstance(metric, Gauge) and metric._callback is not None:
                lines.append(f"{metric.name} {_fmt_float(metric.value())}\n")
            else:
                for key, value in metric._children.items():
                    labels = _render_labels(metric.label_names, key)
                    lines.append(f"{metric.name}{labels} {_fmt_float(value)}\n")
            parts.append("".join(lines))
        return "".join(parts)
