"""组合根：连接信息、实体快照、服务调用。

职责边界：
  * 不管凭据落盘（那是插件层的 `_save_connection`）
  * 不管别名存储（插件层读 aliases.json，这里只接收 dict）
  * 不管文案（这里只抛异常、返回结构化数据）

这样 `client.py` 能在没有宿主、没有文件系统的环境里被单测直接拉起。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .entities import build_entity, filter_controllable, index_registries, resolve_area
from .exceptions import HaAuthError
from .rest import AsyncHaRest, normalize_base_url
from .services import ServiceCall
from .ws import WS_AVAILABLE, AsyncHaWs

# 区域名缺失时的降级说明。放在常量里，面板与日志共用同一句话。
DEGRADED_NO_WS_LIB = "当前环境缺少 websockets 库，区域名不可用"
DEGRADED_WS_FAILED = "无法通过 WebSocket 读取区域注册表"
DEGRADED_NOT_ADMIN = "令牌非管理员，无法读取区域名称"


@dataclass
class ConnectionInfo:
    """一份 HA 连接配置。``token`` 是敏感字段，只在落盘与请求头里出现。"""

    base_url: str = ""
    token: str = ""
    ha_version: str = ""
    location_name: str = ""
    verified_at: float = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "token": self.token,
            "ha_version": self.ha_version,
            "location_name": self.location_name,
            "verified_at": self.verified_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ConnectionInfo:
        if not isinstance(data, dict):
            return cls()
        return cls(
            base_url=str(data.get("base_url") or ""),
            token=str(data.get("token") or ""),
            ha_version=str(data.get("ha_version") or ""),
            location_name=str(data.get("location_name") or ""),
            verified_at=float(data.get("verified_at") or 0.0),
        )

    def public(self) -> dict[str, Any]:
        """给 UI / 日志看的安全视图：令牌只露后 4 位。"""
        tail = self.token[-4:] if len(self.token) >= 4 else ""
        return {
            "base_url": self.base_url,
            "configured": self.configured,
            "ha_version": self.ha_version,
            "location_name": self.location_name,
            "verified_at": self.verified_at,
            "token_tail": f"****{tail}" if tail else "",
        }


@dataclass
class EntitySnapshot:
    """一次实体刷新结果。"""

    entities: list[dict[str, Any]] = field(default_factory=list)
    area_count: int = 0
    ws_used: bool = False
    degraded: str = ""
    ha_version: str = ""
    location_name: str = ""

    def by_entity_id(self) -> dict[str, dict[str, Any]]:
        return {entity["entity_id"]: entity for entity in self.entities if entity.get("entity_id")}

    def domain_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entity in self.entities:
            domain = entity.get("domain", "")
            counts[domain] = counts.get(domain, 0) + 1
        return counts


@dataclass
class _RegistryBundle:
    entity_registry: list[dict[str, Any]] = field(default_factory=list)
    device_registry: list[dict[str, Any]] = field(default_factory=list)
    area_registry: list[dict[str, Any]] = field(default_factory=list)
    degraded: str = ""


async def probe_connection(base_url: str, token: str, *, timeout: float = 10.0) -> ConnectionInfo:
    """校验地址与令牌，成功返回填好版本信息的 ConnectionInfo。

    失败会抛 `HaConnectionError` / `HaAuthError` / `HaError`，由调用方翻成人话。
    这是面板"测试连接"与保存前的唯一校验点——**保存前必须探活**，否则用户会
    存下一个连不上的地址，之后每次控制都要等一次超时。
    """
    rest = AsyncHaRest(base_url, token, timeout=timeout)
    try:
        await rest.ping()
        config = await rest.get_config()
    finally:
        await rest.close()
    return ConnectionInfo(
        base_url=normalize_base_url(base_url),
        token=(token or "").strip(),
        ha_version=str(config.get("version") or ""),
        location_name=str(config.get("location_name") or ""),
        verified_at=time.time(),
    )


class AsyncHaClient:
    """一个 HA 实例的客户端。纯异步。"""

    def __init__(self, connection: ConnectionInfo, *, timeout: float = 10.0) -> None:
        self.connection = connection
        self.timeout = timeout
        self._rest = AsyncHaRest(connection.base_url, connection.token, timeout=timeout)

    # ── 元信息 ──────────────────────────────────────────────────────────
    async def ping(self) -> dict[str, Any]:
        return await self._rest.ping()

    async def get_config(self) -> dict[str, Any]:
        return await self._rest.get_config()

    # ── 注册表 ──────────────────────────────────────────────────────────
    async def _load_registries(self) -> _RegistryBundle:
        """拉三张注册表；任何一步失败都降级，绝不因此让实体刷新失败。

        REST 已经能拿到全部实体和状态，注册表只用来补区域名与别名。它是
        **增强**而不是**前提**——这个区分决定了普通用户令牌能不能用。
        """
        if not WS_AVAILABLE:
            return _RegistryBundle(degraded=DEGRADED_NO_WS_LIB)

        ws = AsyncHaWs(self.connection.base_url, self.connection.token, timeout=max(self.timeout, 15.0))
        try:
            await ws.connect()
        except HaAuthError:
            # WS 握手就鉴权失败，说明令牌本身有问题，交给 REST 侧报。
            return _RegistryBundle(degraded=DEGRADED_WS_FAILED)
        except Exception:
            return _RegistryBundle(degraded=DEGRADED_WS_FAILED)

        try:
            try:
                areas = await ws.area_registry()
                devices = await ws.device_registry()
                entities = await ws.entity_registry()
                return _RegistryBundle(entities, devices, areas)
            except HaAuthError:
                # 普通用户令牌：退到 list_for_display，拿得到 area_id 但拿不到区域名。
                entities = await ws.entity_registry_display()
                return _RegistryBundle(entities, [], [], DEGRADED_NOT_ADMIN)
        except Exception:
            return _RegistryBundle(degraded=DEGRADED_WS_FAILED)
        finally:
            await ws.close()

    # ── 实体 ────────────────────────────────────────────────────────────
    async def collect_entities(
        self,
        *,
        aliases: dict[str, str] | None = None,
        include_read_only: bool = True,
    ) -> EntitySnapshot:
        """拉全量实体并合并注册表信息。

        ``include_read_only=False`` 时剔掉传感器等只读实体的控制候选，但查询
        路径要保留它们——"客厅温度多少"查的就是 sensor。
        """
        states = await self._rest.get_states()
        bundle = await self._load_registries()
        entity_index, device_index, area_names = index_registries(
            bundle.entity_registry, bundle.device_registry, bundle.area_registry
        )
        alias_map = aliases or {}

        entities: list[dict[str, Any]] = []
        for state in states:
            entity_id = str(state.get("entity_id") or "")
            entry = entity_index.get(entity_id)
            area_name, device_name = resolve_area(entry, device_index, area_names)
            entity = build_entity(
                state,
                registry_entry=entry,
                area_name=area_name,
                device_name=device_name,
                plugin_alias=alias_map.get(entity_id, ""),
            )
            if entity is None or entity.get("disabled"):
                continue
            entities.append(entity)

        if not include_read_only:
            entities = filter_controllable(entities)
        else:
            entities = [entity for entity in entities if not entity.get("hidden")]

        entities.sort(key=lambda item: (item.get("area_name", ""), item.get("domain", ""), item.get("name", "")))

        return EntitySnapshot(
            entities=entities,
            area_count=len(area_names),
            ws_used=bundle.degraded == "",
            degraded=bundle.degraded,
            ha_version=self.connection.ha_version,
            location_name=self.connection.location_name,
        )

    async def get_entity_state(self, entity_id: str) -> dict[str, Any] | None:
        return await self._rest.get_state(entity_id)

    # ── 服务调用 ────────────────────────────────────────────────────────
    async def call(self, call: ServiceCall) -> Any:
        return await self._rest.call_service(call.domain, call.service, call.data)

    async def close(self) -> None:
        await self._rest.close()

    async def __aenter__(self) -> AsyncHaClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()
