"""实体视图：把 HA 的 state + 三张注册表合并成一张扁平表。

为什么要合并：HA 把信息拆在四个地方，缺一不可。

    GET /api/states              状态与属性（没有区域名）
    config/entity_registry/list  实体 → 设备 / 实体自带区域 / 用户改名 / 别名
    config/device_registry/list  设备 → 区域（多数实体的区域是"继承自设备"）
    config/area_registry/list    区域 ID → 区域名（中文"客厅"就在这一步）

REST 单独拿不到区域名，这正是必须开 WebSocket 的原因；但 WS 的注册表命令
需要管理员令牌，所以三条命令任一失败都要能降级——最差情况是区域名为空，
匹配退回"按名字子串"，功能不残废。

产出的实体是 **dict**（不是 dataclass）：它要整体写进 entities_cache.json，
dict 省掉一层序列化，也让 `resolver.py` 不必为 HA 改写数据访问方式。
"""

from __future__ import annotations

from typing import Any

from .caps import build_caps

# 进缓存的属性白名单。HA 的 attributes 里塞了 entity_picture（含长 token）、
# attribution 之类的大字段，全量存会让缓存膨胀十几倍，而且这些字段对控制无意义。
KEPT_ATTRIBUTES = {
    "friendly_name",
    "device_class",
    "unit_of_measurement",
    "supported_features",
    "supported_color_modes",
    "color_mode",
    "brightness",
    "color_temp_kelvin",
    "color_temp",
    "min_color_temp_kelvin",
    "max_color_temp_kelvin",
    "effect_list",
    "effect",
    "hvac_modes",
    "hvac_action",
    "min_temp",
    "max_temp",
    "target_temp_step",
    "temperature",
    "current_temperature",
    "fan_modes",
    "fan_mode",
    "preset_modes",
    "preset_mode",
    "swing_modes",
    "current_position",
    "volume_level",
    "is_volume_muted",
    "source",
    "source_list",
    "media_title",
    "media_artist",
    "app_name",
    "percentage",
    "percentage_step",
    "oscillating",
    "operation_list",
    "operation_mode",
    "current_operation",
    "assumed_state",
}

# HA 里表示"设备现在不可达"的两个状态。
OFFLINE_STATES = {"unavailable", "unknown"}


def _slim_attributes(attributes: Any) -> dict[str, Any]:
    if not isinstance(attributes, dict):
        return {}
    return {key: value for key, value in attributes.items() if key in KEPT_ATTRIBUTES}


def _registry_aliases(entry: dict[str, Any]) -> str:
    """注册表里的实体别名（HA 2024.10+）。

    统一成逗号分隔字符串，与 `resolver.py` 的 `_alias_list()` 对齐；
    插件自己的别名存在 aliases.json，合并时排在后面。
    """
    raw = entry.get("aliases")
    if isinstance(raw, (list, tuple)):
        return ",".join(str(item).strip() for item in raw if str(item).strip())
    if isinstance(raw, str):
        return raw.strip()
    return ""


def build_entity(
    state_obj: dict[str, Any],
    *,
    registry_entry: dict[str, Any] | None = None,
    area_name: str = "",
    device_name: str = "",
    plugin_alias: str = "",
) -> dict[str, Any] | None:
    """单个 state 对象 → 实体视图；缺 entity_id 时返回 None。

    名字优先级：friendly_name（HA 已算好"区域 + 设备 + 实体"的最终名）
    > 注册表用户改名 > original_name > entity_id。中文用户看到的房间名
    通常就含在 friendly_name 里，所以它优先。
    """
    entity_id = str(state_obj.get("entity_id") or "").strip()
    if not entity_id or "." not in entity_id:
        return None
    domain = entity_id.split(".", 1)[0]
    attributes = _slim_attributes(state_obj.get("attributes"))
    registry = registry_entry or {}

    name = (
        str(attributes.get("friendly_name") or "").strip()
        or str(registry.get("name") or "").strip()
        or str(registry.get("original_name") or "").strip()
        or entity_id
    )

    aliases = [part for part in (_registry_aliases(registry), plugin_alias) if part]
    state = str(state_obj.get("state") or "")
    caps = build_caps(domain, attributes)

    return {
        "entity_id": entity_id,
        "domain": domain,
        "name": name,
        "area_name": area_name,
        "device_name": device_name,
        "alias": ",".join(aliases),
        "device_class": str(attributes.get("device_class") or ""),
        "state": state,
        "attributes": attributes,
        "caps": caps,
        "risk": caps.get("risk", "low"),
        "is_online": state not in OFFLINE_STATES,
        "disabled": bool(registry.get("disabled_by")),
        "hidden": bool(registry.get("hidden_by")),
    }


def index_registries(
    entity_registry: list[dict[str, Any]] | None,
    device_registry: list[dict[str, Any]] | None,
    area_registry: list[dict[str, Any]] | None,
) -> tuple[dict[str, dict], dict[str, dict], dict[str, str]]:
    """把三张注册表转成按 ID 索引，便于合并时 O(1) 命中。"""
    entities = {
        str(item.get("entity_id")): item
        for item in (entity_registry or [])
        if isinstance(item, dict) and item.get("entity_id")
    }
    devices = {
        str(item.get("id")): item for item in (device_registry or []) if isinstance(item, dict) and item.get("id")
    }
    areas = {
        str(item.get("area_id")): str(item.get("name") or "")
        for item in (area_registry or [])
        if isinstance(item, dict) and item.get("area_id")
    }
    return entities, devices, areas


def resolve_area(
    registry_entry: dict[str, Any] | None,
    devices: dict[str, dict],
    areas: dict[str, str],
) -> tuple[str, str]:
    """解析实体所属区域与设备名。

    区域有两条来源：实体自带 `area_id`，或继承自设备的 `area_id`。HA 前端
    也是这么算的，实体级设置优先。
    """
    entry = registry_entry or {}
    device_id = str(entry.get("device_id") or "")
    device = devices.get(device_id) or {}

    area_id = str(entry.get("area_id") or device.get("area_id") or "")
    area_name = areas.get(area_id, "")

    device_name = str(device.get("name_by_user") or device.get("name") or "")
    return area_name, device_name


def state_facts(entity: dict[str, Any]) -> dict[str, Any]:
    """把实体状态压成与 domain 无关的语义事实，供 i18n 层组装人话。

    这里刻意不产出任何文案：8 语言文案在 `__init__.py` 里按 i18n key 拼，
    否则这个纯函数就得背 8 套翻译。
    """
    domain = entity.get("domain", "")
    state = entity.get("state", "")
    attrs = entity.get("attributes") or {}
    facts: dict[str, Any] = {"domain": domain, "state": state}

    if domain in ("light", "switch", "input_boolean"):
        facts["power"] = state == "on"
        if "brightness" in attrs and isinstance(attrs["brightness"], (int, float)):
            facts["brightness"] = round(float(attrs["brightness"]) * 100 / 255)

    elif domain == "climate":
        facts["power"] = state != "off"
        facts["hvac_mode"] = state
        if isinstance(attrs.get("current_temperature"), (int, float)):
            facts["current_temperature"] = attrs["current_temperature"]
        if isinstance(attrs.get("temperature"), (int, float)):
            facts["target_temperature"] = attrs["temperature"]
        if attrs.get("fan_mode"):
            facts["fan_mode"] = attrs["fan_mode"]

    elif domain == "cover":
        facts["position"] = attrs.get("current_position")
        facts["open"] = state == "open"

    elif domain == "media_player":
        facts["power"] = state not in ("off", "standby", *OFFLINE_STATES)
        if isinstance(attrs.get("volume_level"), (int, float)):
            facts["volume"] = round(float(attrs["volume_level"]) * 100)
        if attrs.get("source"):
            facts["source"] = attrs["source"]
        if attrs.get("media_title"):
            facts["media_title"] = attrs["media_title"]

    elif domain == "lock":
        facts["locked"] = state == "locked"

    elif domain == "fan":
        facts["power"] = state == "on"
        if isinstance(attrs.get("percentage"), (int, float)):
            facts["percentage"] = round(float(attrs["percentage"]))
        if attrs.get("preset_mode"):
            facts["preset"] = attrs["preset_mode"]

    elif domain == "valve":
        facts["open"] = state == "open"
        facts["position"] = attrs.get("current_position")

    elif domain == "water_heater":
        facts["power"] = state != "off"
        facts["operation"] = state
        if isinstance(attrs.get("current_temperature"), (int, float)):
            facts["current_temperature"] = attrs["current_temperature"]
        if isinstance(attrs.get("temperature"), (int, float)):
            facts["target_temperature"] = attrs["temperature"]

    elif domain == "alarm_control_panel":
        facts["armed"] = state.startswith("armed")
        facts["arm_mode"] = state[len("armed_"):] if state.startswith("armed_") else ""

    elif domain in ("sensor", "binary_sensor"):
        facts["value"] = state
        if attrs.get("unit_of_measurement"):
            facts["unit"] = attrs["unit_of_measurement"]

    elif domain == "scene":
        facts["scene"] = True

    return facts


def filter_controllable(entities: list[dict[str, Any]], *, include_read_only: bool = False) -> list[dict[str, Any]]:
    """过滤出可用于控制的实体。

    隐藏实体默认排除（用户自己藏的），禁用实体一定排除；只读传感器默认不进
    控制候选表，但查询功能仍可命中——所以查询路径要把它们读进来。
    """
    from .caps import is_read_only

    out: list[dict[str, Any]] = []
    for entity in entities:
        if entity.get("disabled") or entity.get("hidden"):
            continue
        if not include_read_only and is_read_only(entity.get("domain", "")):
            continue
        out.append(entity)
    return out
