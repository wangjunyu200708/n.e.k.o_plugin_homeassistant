"""实体匹配：从实体快照里按名称匹配。

**来源**：算法复制自 `n.e.k.o_plugin_mijia/nlp/device_matcher.py`，字段与
一处策略有改动：

    字段   room_name → area_name      did → entity_id
           properties/actions → caps（本层不用）
    策略   区域拆分依赖"区域名已知"，而 HA 在非管理员令牌下拿不到区域名。
           此时 `has_area_data` 为假，房间限定会被丢弃、直接走模糊匹配——
           这是有意为之的降级，不是 bug。

匹配优先级不变：精确别名 > 精确名字 > 区域+名字拆分 > 模糊子串。
"""

from dataclasses import dataclass, field
from typing import Optional

# 歧义提示里的 domain 图标。纯显示用，不参与匹配。
_DOMAIN_ICON = {
    "light": "💡",
    "switch": "🔌",
    "climate": "🌡️",
    "cover": "🪟",
    "media_player": "🔊",
    "scene": "🎬",
    "script": "📜",
    "lock": "🔒",
    "sensor": "📊",
    "binary_sensor": "📊",
    "fan": "🌀",
    "vacuum": "🤖",
}


@dataclass
class MatchResult:
    """实体匹配结果。

    status: "ok"（唯一命中）| "ambiguous"（多命中）| "not_found"（无匹配）
    candidates: 失败/歧义时的候选摘要，供结构化错误 detail 直接透给模型；
    只放定位信息，不放 attributes（控制 token 体积）。
    """

    status: str
    devices: list = field(default_factory=list)
    message: str = ""
    candidates: list = field(default_factory=list)

    @property
    def device(self) -> Optional[dict]:
        """唯一命中时返回实体 dict，否则 None。"""
        return self.devices[0] if self.devices else None


def summarize_candidates(entities: list[dict], limit: int = 20) -> list[dict]:
    """把实体列表压成候选摘要（错误 detail 用）。"""
    out: list[dict] = []
    for entity in entities[:limit]:
        out.append(
            {
                "name": entity.get("name", ""),
                "area": entity.get("area_name", ""),
                "domain": entity.get("domain", ""),
                "entity_id": entity.get("entity_id", ""),
                "alias": entity.get("alias", ""),
            }
        )
    return out


def _normalize(name: str) -> str:
    return name.lower().strip().replace("的", "")


def _alias_list(entity: dict) -> list[str]:
    alias = entity.get("alias", "")
    if not alias:
        return []
    return [part.strip().lower() for part in alias.split(",") if part.strip()]


def _label(entity: dict) -> str:
    """区域 + 名字 + 别名，用于歧义提示。"""
    area = entity.get("area_name", "")
    name = entity.get("name", "未知")
    label = f"{area} {name}" if area else name
    alias = entity.get("alias", "")
    if alias:
        label += f" (别名: {alias})"
    icon = _DOMAIN_ICON.get(entity.get("domain", ""), "•")
    return f"{icon} {label}"


def format_ambiguous_message(query: str, entities: list[dict]) -> str:
    """格式化多实体歧义提示。"""
    lines = [f"找到 {len(entities)} 个匹配 '{query}' 的设备："]
    for index, entity in enumerate(entities, 1):
        status = "🟢" if entity.get("is_online") else "🔴"
        lines.append(f"  {index}. {status} {_label(entity)}")
    lines.append("请用区域名+设备名精确指定，如 '客厅吸顶灯'")
    return "\n".join(lines)


def _list_all_entities(entities: list[dict], limit: int = 60) -> str:
    lines = [f"  • {_label(entity)}" for entity in entities[:limit]]
    if len(entities) > limit:
        lines.append(f"  …另有 {len(entities) - limit} 个")
    return "\n".join(lines)


def match_devices(
    name: str,
    devices: list[dict],
    *,
    api_room_map: Optional[dict[str, str]] = None,
    device_room_map: Optional[dict[str, str]] = None,
) -> MatchResult:
    """统一实体匹配。

    Args:
        name: 用户输入的实体名/别名/区域+名字。
        devices: 实体快照列表。
        api_room_map / device_room_map: **仅为接口兼容保留**。HA 版本的区域
            名在拉取实体时已合并进 ``area_name``，不需要运行期再补。
    """
    if not devices:
        return MatchResult("not_found", [], "设备列表为空，请先在面板完成 Home Assistant 连接")

    name_lower = _normalize(name)

    # ── 1) 精确匹配：别名优先于名字 ──
    exact: list[dict] = []
    for entity in devices:
        if name_lower in _alias_list(entity):
            exact.append(entity)
            continue
        if _normalize(entity.get("name", "")) == name_lower:
            exact.append(entity)

    if len(exact) == 1:
        return MatchResult("ok", exact)
    if len(exact) > 1:
        return MatchResult(
            "ambiguous",
            exact,
            format_ambiguous_message(name, exact),
            candidates=summarize_candidates(exact),
        )

    # ── 2) 区域 + 名字拆分 ──
    area_map: dict[str, str] = {}
    for entity in devices:
        area = entity.get("area_name", "")
        if area:
            area_map[area.lower()] = area

    has_area_data = bool(area_map)
    area_matched: list[dict] = []
    for area_lower in area_map:
        device_part = None
        if name_lower.startswith(area_lower):
            device_part = name_lower[len(area_lower) :].strip()
        elif name_lower.endswith(area_lower):
            device_part = name_lower[: -len(area_lower)].strip()

        if not device_part:
            continue
        if not has_area_data:
            break

        for entity in devices:
            if entity.get("area_name", "").lower() != area_lower:
                continue
            entity_name = _normalize(entity.get("name", ""))
            # 双向子串：别名"床头台灯"能被房间限定输入"卧室床头"命中
            if device_part in entity_name or any(
                device_part in alias or alias in device_part for alias in _alias_list(entity)
            ):
                area_matched.append(entity)

    if len(area_matched) == 1:
        return MatchResult("ok", area_matched)
    if len(area_matched) > 1:
        return MatchResult(
            "ambiguous",
            area_matched,
            format_ambiguous_message(name, area_matched),
            candidates=summarize_candidates(area_matched),
        )

    # ── 3) 模糊匹配（双向子串）──
    fuzzy: list[dict] = []
    for entity in devices:
        entity_name = _normalize(entity.get("name", ""))
        if entity_name and (name_lower in entity_name or entity_name in name_lower):
            fuzzy.append(entity)
            continue
        if any(name_lower in alias or alias in name_lower for alias in _alias_list(entity)):
            fuzzy.append(entity)

    if len(fuzzy) == 1:
        return MatchResult("ok", fuzzy)
    if len(fuzzy) > 1:
        return MatchResult(
            "ambiguous",
            fuzzy,
            format_ambiguous_message(name, fuzzy),
            candidates=summarize_candidates(fuzzy),
        )

    # ── 4) 完全无匹配，列出可选项 ──
    return MatchResult(
        "not_found",
        [],
        f"未找到匹配 '{name}' 的设备。当前可控制设备：\n" + _list_all_entities(devices),
        candidates=summarize_candidates(devices),
    )


def find_scene(devices: list[dict], keywords: list[str]) -> Optional[dict]:
    """按关键词序列找场景/脚本实体，返回第一个命中的。

    HA 里场景是 ``scene.xxx`` / ``script.xxx`` 实体，没有米家那种独立
    "场景服务"，所以"我回家了"要先落到某个场景实体上。各家命名不同
    （"回家"/"到家"/"我回来了"），因此按候选词顺序逐个试。
    """
    scenes = [entity for entity in devices if entity.get("domain") in ("scene", "script")]
    for keyword in keywords:
        target = keyword.strip().lower()
        if not target:
            continue
        for entity in scenes:
            if target in entity.get("name", "").lower():
                return entity
        for entity in scenes:
            if any(target in alias or alias in target for alias in _alias_list(entity)):
                return entity
    return None
