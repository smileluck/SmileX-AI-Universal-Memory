"""Smoke test: cachebox (L0 working memory).

⚠️ API 漂移告警(重大):
   主文档 §7.2 引用 `TTLPrefixCache(...).set(k, v)`,但 cachebox 6.1.2 实际 API:
   - 类名:`TTLCache`(用 `global_ttl` 参数)/ `LRUCache` / `VTTLCache`(per-key ttl)
   - 没有 TTL+LRU 合并类(`TTLPrefixCache` 不存在)
   - 写入方法:`.insert(k, v)`(不是 `.set()`)
   - 读取方法:`.get(k)`(一致)

   详见 docs/design/modules/EXECUTION_PLAN_GAPS.md §3.9。
"""

import time

from cachebox import LRUCache, TTLCache


def test_cachebox_ttl_set_get():
    cache = TTLCache(maxsize=100, global_ttl=60)
    cache.insert("k1", "v1")
    assert cache.get("k1") == "v1"


def test_cachebox_ttl_expiration():
    """验证 TTL 过期生效."""
    cache = TTLCache(maxsize=100, global_ttl=0.5)
    cache.insert("k_expire", "v")
    assert cache.get("k_expire") == "v"
    time.sleep(0.6)
    assert cache.get("k_expire") is None, "0.6s 后 TTL 应该过期"


def test_cachebox_lru_eviction():
    """验证 LRUCache maxsize 满后淘汰."""
    cache = LRUCache(maxsize=2)
    cache.insert("a", 1)
    cache.insert("b", 2)
    # 访问 a,使 b 成为 LRU
    assert cache.get("a") == 1
    # 插入 c,应该淘汰 b
    cache.insert("c", 3)
    assert cache.get("b") is None, "b 应该被 LRU 淘汰"
    assert cache.get("a") == 1
    assert cache.get("c") == 3


def test_cachebox_full_api_inventory():
    """辅助: 验证 cachebox 6.1.2 实际导出的类,确认文档假设的 TTLPrefixCache 不存在."""
    import cachebox

    cache_classes = [
        x for x in dir(cachebox)
        if x.endswith("Cache") and not x.startswith("_")
    ]
    assert "TTLCache" in cache_classes
    assert "LRUCache" in cache_classes
    # 文档假设的 TTLPrefixCache 实际不存在
    assert "TTLPrefixCache" not in cache_classes, (
        "如果 cachebox 后续版本加入了 TTLPrefixCache,本测试与文档假设需重新评估"
    )
