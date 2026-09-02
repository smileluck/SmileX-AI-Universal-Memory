```
=== LongMemEval mem0 兼容口径(source=original) — smilex-ai-memory(embedder=sentence-transformers) ===
answerer=glm-5.3-flash judge=glm-5.3-flash (mem0 官方为 gpt-5/gpt-5,比较时注意)

== cutoff 10 — n=30, Overall=90.0 % ==
    knowledge-update           5 题  80.0 %
    multi-session              5 题  80.0 %
    single-session-assistant   5 题  80.0 %
    single-session-preference   5 题  100.0 %
    single-session-user        5 题  100.0 %
    temporal-reasoning         5 题  100.0 %

== cutoff 20 — n=30, Overall=96.7 % ==
    knowledge-update           5 题  100.0 %
    multi-session              5 题  80.0 %
    single-session-assistant   5 题  100.0 %
    single-session-preference   5 题  100.0 %
    single-session-user        5 题  100.0 %
    temporal-reasoning         5 题  100.0 %

== cutoff 50 — n=30, Overall=86.7 % ==
    knowledge-update           5 题  60.0 %
    multi-session              5 题  60.0 %
    single-session-assistant   5 题  100.0 %
    single-session-preference   5 题  100.0 %
    single-session-user        5 题  100.0 %
    temporal-reasoning         5 题  100.0 %

== cutoff 200 (headline) — n=30, Overall=93.3 % ==
    knowledge-update           5 题  80.0 %
    multi-session              5 题  80.0 %
    single-session-assistant   5 题  100.0 %
    single-session-preference   5 题  100.0 %
    single-session-user        5 题  100.0 %
    temporal-reasoning         5 题  100.0 %

Total Time 6.6 min
```
