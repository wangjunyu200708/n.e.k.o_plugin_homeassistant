"""HA 的 WebSocket 部分：只为拿注册表（区域名 / 设备名 / 实体别名）。

REST 拿不到区域名，而中文房间语义（"客厅的灯"）全靠它。可用的命令：

    config/area_registry/list           区域 ID → 名字          （需管理员）
    config/device_registry/list         设备 → 区域             （需管理员）
    config/entity_registry/list         实体 → 设备/区域/别名    （需管理员）
    config/entity_registry/list_for_display  同上但字段压缩      （普通用户可用）

普通用户令牌下前三条会 `unauthorized`，此时降级到 `list_for_display`：仍能拿到
实体的区域 ID 与设备 ID，拿不到区域**名称**——匹配层就退回按名字子串，功能
不残废，只是"客厅灯"这类房间限定不如管理员令牌精准。

`websockets` 缺失时整体不启用（`WS_AVAILABLE=False`），由 client 层走降级路径。
插件不能因为一个可选优化就起不来。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .exceptions import HaAuthError, HaConnectionError, HaError, HaServiceError
from .rest import normalize_base_url

WS_AVAILABLE = True
try:  # websockets >= 13 的新 asyncio 客户端；旧版回落到顶层 connect
    from websockets.asyncio.client import connect as _ws_connect
except Exception:  # pragma: no cover - 取决于环境
    try:
        from websockets import connect as _ws_connect  # type: ignore[no-redef]
    except Exception:  # pragma: no cover
        _ws_connect = None  # type: ignore[assignment]
        WS_AVAILABLE = False

# 注册表可能上千条，默认 1MiB 上限不够。
_MAX_MESSAGE = 32 * 1024 * 1024


def ws_url(base_url: str) -> str:
    """``http://h:8123/`` → ``ws://h:8123/api/websocket``。"""
    parts = urlsplit(normalize_base_url(base_url))
    scheme = "wss" if parts.scheme == "https" else "ws"
    path = f"{parts.path.rstrip('/')}/api/websocket"
    return urlunsplit((scheme, parts.netloc, path, "", ""))


class AsyncHaWs:
    """一次连接、若干次命令、立即断开。不做长连接订阅。

    控制是低频动作，每次刷新实体时新建一条 WS 比维护重连逻辑更省事，也不会
    因为 HA 重启留下一堆僵尸连接。
    """

    def __init__(self, base_url: str, token: str, *, timeout: float = 15.0) -> None:
        self.url = ws_url(base_url)
        self._token = (token or "").strip()
        self.timeout = timeout
        self._conn: Any = None
        self._next_id = 0

    @property
    def available(self) -> bool:
        return WS_AVAILABLE and _ws_connect is not None

    async def connect(self) -> None:
        if not self.available:
            raise HaError("当前环境缺少 websockets 库，无法查询区域注册表")
        try:
            self._conn = await _ws_connect(
                self.url,
                open_timeout=self.timeout,
                close_timeout=2,
                max_size=_MAX_MESSAGE,
            )
        except Exception as exc:
            raise HaConnectionError(f"无法建立 WebSocket 连接（{self.url}）：{exc}") from exc

        hello = await self._recv()
        kind = hello.get("type")
        if kind == "auth_required":
            await self._send({"type": "auth", "access_token": self._token})
            reply = await self._recv()
            kind = reply.get("type")
            if kind == "auth_invalid":
                raise HaAuthError(f"WebSocket 鉴权失败：{reply.get('message') or '令牌无效'}")
            if kind != "auth_ok":
                raise HaError(f"WebSocket 鉴权返回了意外消息：{kind}")
        elif kind != "auth_ok":
            raise HaError(f"WebSocket 握手返回了意外消息：{kind}")

    async def _send(self, payload: dict[str, Any]) -> None:
        assert self._conn is not None
        try:
            await self._conn.send(json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            raise HaConnectionError(f"WebSocket 发送失败：{exc}") from exc

    async def _recv(self) -> dict[str, Any]:
        assert self._conn is not None
        try:
            raw = await asyncio.wait_for(self._conn.recv(), timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            raise HaConnectionError("WebSocket 等待响应超时") from exc
        except Exception as exc:
            raise HaConnectionError(f"WebSocket 接收失败：{exc}") from exc
        try:
            message = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise HaError("WebSocket 返回了非 JSON 数据") from exc
        return message if isinstance(message, dict) else {}

    async def command(self, command_type: str, **payload: Any) -> Any:
        """发一条命令并等它的 result。"""
        self._next_id += 1
        message_id = self._next_id
        body: dict[str, Any] = {"id": message_id, "type": command_type}
        body.update(payload)
        await self._send(body)

        while True:
            reply = await self._recv()
            # 事件订阅推送会插在中间，按 id 对齐后忽略无关帧。
            if reply.get("id") != message_id or reply.get("type") != "result":
                continue
            if not reply.get("success"):
                error = reply.get("error") or {}
                code = str(error.get("code") or "unknown_error")
                text = str(error.get("message") or code)
                if code in ("unauthorized", "not_allowed"):
                    raise HaAuthError(f"权限不足：{text}")
                raise HaServiceError(f"命令 {command_type} 失败：{text}", detail={"code": code})
            return reply.get("result")

    # ── 注册表 ──────────────────────────────────────────────────────────
    async def area_registry(self) -> list[dict[str, Any]]:
        return self._as_list(await self.command("config/area_registry/list"))

    async def device_registry(self) -> list[dict[str, Any]]:
        return self._as_list(await self.command("config/device_registry/list"))

    async def entity_registry(self) -> list[dict[str, Any]]:
        return self._as_list(await self.command("config/entity_registry/list"))

    async def entity_registry_display(self) -> list[dict[str, Any]]:
        """非管理员可用的压缩版注册表。

        返回字段是缩写：``ei``(entity_id) / ``ai``(area_id) / ``di``(device_id) /
        ``en``(name) / ``hn``(has_entity_name) / ``hb``(hidden) / ``lb``(labels)。
        这里就地展开成完整键名，让上层只认一套字段。
        """
        raw = self._as_list(await self.command("config/entity_registry/list_for_display"))
        out: list[dict[str, Any]] = []
        for item in raw:
            entity_id = item.get("ei") or item.get("entity_id")
            if not entity_id:
                continue
            out.append(
                {
                    "entity_id": entity_id,
                    "area_id": item.get("ai") or item.get("area_id") or "",
                    "device_id": item.get("di") or item.get("device_id") or "",
                    "name": item.get("en") or item.get("name") or "",
                    "hidden_by": "user" if item.get("hb") else None,
                    "disabled_by": None,
                }
            )
        return out

    @staticmethod
    def _as_list(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    async def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                # 关闭失败不影响结果，连接终会被 HA 侧回收。
                pass

    async def __aenter__(self) -> AsyncHaWs:
        await self.connect()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()
