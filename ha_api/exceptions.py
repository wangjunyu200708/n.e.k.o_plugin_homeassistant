"""HA 接入层异常。

区分三类，因为上层要给用户完全不同的处置建议：

- `HaAuthError`       令牌无效 / 过期 / 权限不足 → 去面板重新生成令牌
- `HaConnectionError` 地址不通 / 超时 / TLS 错  → 检查 URL、端口、网络
- `HaServiceError`    服务被 HA 或设备拒绝     → 换指令或看设备日志

把连接失败混进鉴权失败会让用户以为要重新登录，实际只是地址写错。
"""

from __future__ import annotations


class HaError(Exception):
    """接入层异常基类。"""

    code = "ha_error"


class HaAuthError(HaError):
    """401/403：令牌无效、已吊销，或该用户没有此操作权限。"""

    code = "ha_auth_failed"


class HaConnectionError(HaError):
    """连不上：DNS 失败、连接被拒、超时、证书错误。"""

    code = "ha_unreachable"


class HaServiceError(HaError):
    """服务调用被拒绝（HTTP 400/500，或 WS success=false）。"""

    code = "ha_service_failed"

    def __init__(self, message: str, *, detail: object | None = None) -> None:
        super().__init__(message)
        self.detail = detail
