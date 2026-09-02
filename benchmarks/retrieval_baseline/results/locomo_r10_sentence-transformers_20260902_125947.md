```
=== 检索基线对比 R@10(locomo) — raw BGE-M3 KNN vs smilex recall ===
题目数: 1540(adversarial 不计入)
smilex_recall     = 融合排名前 top-k 个 source(与 raw 同口径)
smilex_recall_all = recall 实际返回的全部 source(双通道去重后 ~2x10)
raw_bge_m3     (skipped)
smilex_recall  Overall 1496/1536 = 97.4 %
    multi_hop                272/282 = 96.5 %
    open_domain              77/92 = 83.7 %
    single_hop               833/841 = 99.0 %
    temporal                 314/321 = 97.8 %
smilex_recall_all Overall 1513/1536 = 98.5 %
    multi_hop                277/282 = 98.2 %
    open_domain              83/92 = 90.2 %
    single_hop               837/841 = 99.5 %
    temporal                 316/321 = 98.4 %
Total Time       114.9 min
```
