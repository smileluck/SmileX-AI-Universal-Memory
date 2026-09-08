"""HTTP API Key 鉴权中间件(§15.4,默认关闭).

设计:
- 挂在主 FastAPI app 最外层(后注册的 @app.middleware 先执行)——单点
  覆盖 /api/*、/metrics、面板静态资源与兜底 mount 的 MCP 子应用
- 双方案取 key: ``Authorization: Bearer <key>`` 或 ``X-API-Key: <key>``
  (curl/自研客户端用前者,MCP 客户端 headers 配置用后者更直观)
- ``hmac.compare_digest`` 恒时比较,防时序侧信道
- 豁免路径: ``/``、``/static/*``(面板首屏无法带 header)、
  ``/api/health``(daemon 就绪探测/status/doctor 依赖;未带有效 key 时
  端点自行返回裁剪载荷,见 api.health)
- 对豁免路径仍会校验 key 并写 ``request.state.auth_ok``——health 据此
  决定是否返回完整配置摘要
- 未启用(config.effective_api_key 为 None)时零开销直通,行为与历史
  版本完全一致
"""

from __future__ import annotations

import hmac
from typing import Any

from .config import ServerConfig

# 豁免前缀(面板静态资源)与精确路径(健康探测)
_EXEMPT_EXACT = frozenset({"/", "/api/health"})
_EXEMPT_PREFIXES = ("/static/",)


def extract_api_key(request: Any) -> str | None:
    """从请求头提取 key: Authorization: Bearer 优先,X-API-Key 兜底."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        candidate: str = str(auth[7:].strip())
        if candidate:
            return candidate
    header_key: str | None = request.headers.get("x-api-key")
    return header_key or None


def is_exempt_path(path: str) -> bool:
    """是否豁免路径(面板静态资源 + 健康探测)."""
    return path in _EXEMPT_EXACT or path.startswith(_EXEMPT_PREFIXES)


def install_auth_middleware(app: Any, config: ServerConfig) -> None:
    """在 FastAPI app 上安装 API Key 鉴权中间件(未配置 key 时不动).

    必须在其余请求中间件(如指标计数)注册**之后**调用——Starlette 中
    后注册的中间件包在外层,鉴权因此先于计数执行(未授权请求不进业务
    与指标)。
    """
    expected = config.effective_api_key
    if not expected:
        return

    @app.middleware("http")  # type: ignore[untyped-decorator]  # app 为 Any(免 fastapi 硬依赖)
    async def api_key_guard(request: Any, call_next: Any) -> Any:
        from fastapi.responses import JSONResponse

        provided = extract_api_key(request)
        # 恒时比较;对豁免路径只记录校验结果供端点裁剪,不拒绝
        request.state.auth_ok = (
            provided is not None
            and hmac.compare_digest(provided.encode(), expected.encode())
        )
        if request.state.auth_ok or is_exempt_path(request.scope.get("path", "")):
            return await call_next(request)
        return JSONResponse(
            {"detail": "unauthorized: 缺少或错误的 API Key"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
