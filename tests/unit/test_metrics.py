"""Unit tests for 零依赖指标注册表(§15.3 metrics)."""

from __future__ import annotations

import pytest

from smilex.memory.observability.metrics import (
    Counter,
    Gauge,
    Histogram,
    MetricsRegistry,
)


def test_counter_with_labels():
    reg = MetricsRegistry()
    c = reg.counter("t_total", "测试计数", ("status",))
    c.inc(status="saved")
    c.inc(status="saved")
    c.inc(2.5, status="conflict")
    assert c.value(status="saved") == 2
    assert c.value(status="conflict") == 2.5
    assert c.value(status="never") == 0
    out = reg.render_prometheus()
    assert 't_total{status="saved"} 2' in out
    assert 't_total{status="conflict"} 2.5' in out
    assert "# TYPE t_total counter" in out


def test_counter_rejects_negative_and_bad_labels():
    c = Counter("t_total", "h", ("status",))
    with pytest.raises(ValueError, match="递增"):
        c.inc(-1, status="a")
    with pytest.raises(ValueError, match="未声明"):
        c.inc(wrong="x")
    with pytest.raises(ValueError, match="缺少"):
        c.inc()


def test_gauge_set_and_callback():
    reg = MetricsRegistry()
    g = reg.gauge("depth", "深度")
    g.set(5)
    g.set(7)
    assert g.value() == 7

    state = {"v": 1}
    lazy = reg.gauge("lazy", "惰性", callback=lambda: state["v"])
    state["v"] = 42
    assert lazy.value() == 42
    out = reg.render_prometheus()
    assert "lazy 42" in out
    with pytest.raises(ValueError, match="不可 set"):
        lazy.set(1)


def test_histogram_buckets_cumulative():
    reg = MetricsRegistry()
    h = reg.histogram("lat", "延迟", buckets=(0.01, 0.1, 1.0))
    h.observe(0.005)
    h.observe(0.05)
    h.observe(5.0)
    out = reg.render_prometheus()
    # 累计桶: 0.01→1, 0.1→2, 1.0→2, +Inf 由 _count 承载
    assert 'lat_bucket{le="0.01"} 1' in out
    assert 'lat_bucket{le="0.1"} 2' in out
    assert 'lat_bucket{le="1"} 2' in out
    assert "lat_count 3" in out
    assert "lat_sum 5.055" in out


def test_histogram_labeled_series_isolated():
    reg = MetricsRegistry()
    h = reg.histogram("lat", "延迟", label_names=("op",))
    h.observe(0.1, op="write")
    h.observe(1.0, op="recall")
    out = reg.render_prometheus()
    assert 'lat_count{op="write"} 1' in out
    assert 'lat_count{op="recall"} 1' in out


def test_registry_same_name_idempotent_and_type_conflict():
    reg = MetricsRegistry()
    c1 = reg.counter("same", "h")
    c2 = reg.counter("same", "h")
    assert c1 is c2
    with pytest.raises(ValueError, match="已注册"):
        reg.gauge("same", "h")
    with pytest.raises(ValueError, match="已注册"):
        reg.histogram("same", "h")


def test_invalid_names_and_buckets():
    reg = MetricsRegistry()
    with pytest.raises(ValueError, match="指标名"):
        reg.counter("not-a-metric", "h")
    with pytest.raises(ValueError, match="严格递增"):
        Histogram("lat", "h", buckets=(1.0, 0.5))


def test_label_value_escaping():
    reg = MetricsRegistry()
    c = reg.counter("esc", "h", ("path",))
    c.inc(path='/api "x"\n')
    out = reg.render_prometheus()
    assert 'path="/api \\"x\\"\\n"' in out


def test_no_labels_renders_bare_metric():
    reg = MetricsRegistry()
    reg.gauge("plain", "h").set(3)
    assert "plain 3" in reg.render_prometheus()


def test_counter_gauge_mypy_types_independent():
    reg = MetricsRegistry()
    assert isinstance(reg.counter("a", "h"), Counter)
    assert isinstance(reg.gauge("b", "h"), Gauge)
