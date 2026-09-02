```
=== LoCoMo mem0 兼容口径 — smilex-ai-memory(embedder=sentence-transformers) ===
answerer=glm-5.3-flash judge=glm-5.3-flash (mem0 官方为 gpt-5/gpt-5,比较时注意)

== cutoff 10 — n=90, Overall=97.8 % ==
    multi-hop                 36 题  97.2 %
    open-domain                7 题  85.7 %
    single-hop                 2 题  100.0 %
    temporal                  45 题  100.0 %

== cutoff 20 — n=90, Overall=97.8 % ==
    multi-hop                 36 题  100.0 %
    open-domain                7 题  71.4 %
    single-hop                 2 题  100.0 %
    temporal                  45 题  100.0 %

== cutoff 50 — n=90, Overall=98.9 % ==
    multi-hop                 36 题  100.0 %
    open-domain                7 题  85.7 %
    single-hop                 2 题  100.0 %
    temporal                  45 题  100.0 %

== cutoff 200 (headline) — n=90, Overall=98.9 % ==
    multi-hop                 36 题  100.0 %
    open-domain                7 题  85.7 %
    single-hop                 2 题  100.0 %
    temporal                  45 题  100.0 %

Total Time 131.0 min
```
