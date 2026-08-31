```
=== 检索基线对比 R@10(locomo) — raw BGE-M3 KNN vs smilex recall ===
题目数: 152(adversarial 不计入)
smilex_recall     = 融合排名前 top-k 个 source(与 raw 同口径)
smilex_recall_all = recall 实际返回的全部 source(双通道去重后 ~2x10)
raw_bge_m3     Overall 130/150 = 86.7 %
    multi_hop                34/37 = 91.9 %
    open_domain              62/70 = 88.6 %
    single_hop               25/32 = 78.1 %
    temporal                 9/11 = 81.8 %
smilex_recall  Overall 145/150 = 96.7 %
    multi_hop                37/37 = 100.0 %
    open_domain              69/70 = 98.6 %
    single_hop               29/32 = 90.6 %
    temporal                 10/11 = 90.9 %
smilex_recall_all Overall 147/150 = 98.0 %
    multi_hop                37/37 = 100.0 %
    open_domain              70/70 = 100.0 %
    single_hop               30/32 = 93.8 %
    temporal                 10/11 = 90.9 %
Total Time       0.7 min
```
