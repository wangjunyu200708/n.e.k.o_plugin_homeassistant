"""Home Assistant 接入层。

与 `mijia_api/` 完全无关：HA 的实体模型（entity_id + attributes）与米家的
（siid/aiid + properties）不同构，**没有一行可以复用**。

分层：

    exceptions  异常（鉴权 / 连接 / 服务调用）
    caps        按 domain 归一化能力表（本层最核心的适配器）
    entities    state + 注册表 → 统一实体视图（dict）
    rest        HTTP 部分：探测 / 状态 / 调用服务
    ws          WebSocket 部分：区域 / 设备 / 实体注册表
    services    实体 + 语义动作 → HA 服务调用与参数
    client      组合根：连接、实体缓存、别名、状态查询
"""

from .client import AsyncHaClient, ConnectionInfo
from .exceptions import HaAuthError, HaConnectionError, HaError, HaServiceError

__all__ = [
    "AsyncHaClient",
    "ConnectionInfo",
    "HaAuthError",
    "HaConnectionError",
    "HaError",
    "HaServiceError",
]
