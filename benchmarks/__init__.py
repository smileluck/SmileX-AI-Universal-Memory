"""benchmarks — 记忆质量评测脚本(locomo / longmemeval / mem0_compat / retrieval_baseline).

非 pytest 资产,不参与包构建;各入口脚本头部有
``sys.path.insert(0, str(Path(__file__).resolve().parents[1]))``
以 ``uv run python benchmarks/<dir>/<script>.py`` 方式运行时可跨目录导入
``_shared`` 与彼此的 ingest 模块.
"""
