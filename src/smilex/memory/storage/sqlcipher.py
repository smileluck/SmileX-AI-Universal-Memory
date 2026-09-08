"""SQLCipher 适配(§15.4 文件加密,实验性).

关键约束: aiosqlite 硬绑定标准库 ``sqlite3``,而 SQLCipher 是 SQLite 的
整库 fork(不是可加载扩展)——要让 ``PRAGMA key`` 生效,解释器层面的
sqlite3 模块必须是 SQLCipher 构建。两条落地路径:

1. **pysqlcipher3 shim(本模块)**: 安装 extra
   ``pip install 'smilex-ai-memory[encryption]'``(需系统 libsqlcipher:
   macOS ``brew install sqlcipher`` / Debian ``apt install libsqlcipher-dev``
   + ``libsqlcipher0``),然后 ``install_shim()`` 把 aiosqlite 内部引用的
   ``sqlite3`` 换成 pysqlcipher3 的 dbapi2。进程级替换,仅在明确启用
   加密时调用。
2. **发行版 sqlcipher 构建的解释器**: 一些环境(conda-forge / 定制构建)
   的 sqlite3 本身就是 SQLCipher,无需 shim,直接配 key 即可。

无论哪条路径,SQLiteEngine.initialize() 的 ``PRAGMA cipher_version``
校验都会把关: 不是 SQLCipher 时立刻报错中止,绝不静默写明文库。

已知限制(实验性原因): pysqlcipher3 与 aiosqlite 的组合未经官方支持,
替换后 sqlite-vec 扩展是否可加载取决于构建;混合负载(加密 + 向量)
请先小规模验证。
"""

from __future__ import annotations

import aiosqlite.core

_SHIMMED = False


def sqlcipher_available() -> bool:
    """pysqlcipher3 是否已安装(仅探测,不做替换)."""
    try:
        import pysqlcipher3.dbapi2  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        return False
    return True


def install_shim() -> bool:
    """把 aiosqlite 的 sqlite3 引用替换为 pysqlcipher3.dbapi2.

    幂等;未安装 pysqlcipher3 时返回 False(调用方应回退到明文并提示).
    必须在创建任何 aiosqlite 连接**之前**调用(连接持有原生模块引用).
    """
    global _SHIMMED
    if _SHIMMED:
        return True
    try:
        from pysqlcipher3 import dbapi2 as cipher_sqlite3
    except ImportError:
        return False
    # aiosqlite 未声明 sqlite3 为公开属性,shim 本就是深度替换
    aiosqlite.core.sqlite3 = cipher_sqlite3  # type: ignore[attr-defined]
    _SHIMMED = True
    return True
