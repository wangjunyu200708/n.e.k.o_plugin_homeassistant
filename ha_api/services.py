"""语义动作 → HA 服务调用。

这一层是 HA 专有知识的唯一落点：量纲换算、字段名、危险动作判定。NLP 层只
产出"亮度 50%"这种人话语义，校验层只读 caps——两者都不碰 HA 细节。

量纲对照（本层负责换算，其它层一律 0-100 / 摄氏度 / 开尔文）：

    brightness_pct    light         0-100   ← 0 等价关灯，改发 turn_off
    color_temp_kelvin light         开尔文
    rgb_color         light         [r, g, b] 0-255
    temperature       climate       摄氏度
    position          cover         0-100
    volume_level      media_player  0.0-1.0  ← 由 0-100 除以 100

**关于 cap 参数**：`prop_call()` 接收已算好的 caps 键，不自己判断"亮度"该落在
哪个能力上。原因是"模式"在 climate/light/media_player 下分别指 HVAC 模式、
灯效、输入源，这套消歧必须与校验层用同一份逻辑，否则会出现「用 A 能力校验、
按 B 能力执行」。单一真源在 `nlp/capability.py::cap_for_prop()`。

返回 None 表示"该实体不支持这个动作"——调用方要据此报 `attribute_not_found`
并列出真正支持的属性，而不是静默降级。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 媒体动作动词 → HA 服务。
MEDIA_SERVICES = {
    "播放": ("media_play", {}),
    "继续播放": ("media_play", {}),
    "暂停": ("media_pause", {}),
    "停止": ("media_stop", {}),
    "下一首": ("media_next_track", {}),
    "下一曲": ("media_next_track", {}),
    "下一个": ("media_next_track", {}),
    "上一首": ("media_previous_track", {}),
    "上一曲": ("media_previous_track", {}),
    "上一个": ("media_previous_track", {}),
    "静音": ("volume_mute", {"is_volume_muted": True}),
    "取消静音": ("volume_mute", {"is_volume_muted": False}),
}

# 危险服务：命中即需要用户二次确认。
_DANGEROUS_CALLS = {
    ("lock", "unlock"),
    ("lock", "open"),
    ("alarm_control_panel", "alarm_disarm"),
}

# 打开即暴露物理风险的 cover 类型（车库门 / 大门 / 栅栏门）。
_HAZARD_COVER_CLASSES = {"garage", "garage_door", "gate", "door"}


@dataclass
class ServiceCall:
    """一次待执行的 HA 服务调用。"""

    domain: str
    service: str
    data: dict[str, Any] = field(default_factory=dict)
    dangerous: bool = False
    # 语义标签，供文案与确认提示使用（如 "亮度 50%" / "解锁"）
    label: str = ""

    @property
    def service_id(self) -> str:
        return f"{self.domain}.{self.service}"


def is_dangerous(entity: dict[str, Any], domain: str, service: str) -> bool:
    """判定一次调用是否需要二次确认。

    三条来源：锁的 unlock/open、警报面板的 disarm、以及**车库/大门类** cover
    的 open_cover。窗帘不算——"打开窗帘"没有物理风险，弹确认只会烦人。
    """
    if (domain, service) in _DANGEROUS_CALLS:
        return True
    if domain == "cover" and service == "open_cover":
        device_class = str((entity.get("caps") or {}).get("device_class") or "").lower()
        return device_class in _HAZARD_COVER_CLASSES
    return False


def _call(domain: str, service: str, entity: dict[str, Any], data: dict[str, Any], label: str) -> ServiceCall:
    payload: dict[str, Any] = {"entity_id": entity.get("entity_id", "")}
    payload.update(data)
    return ServiceCall(
        domain=domain,
        service=service,
        data=payload,
        dangerous=is_dangerous(entity, domain, service),
        label=label,
    )


# ── 开关类 ──────────────────────────────────────────────────────────────
def power_call(entity: dict[str, Any], on: bool) -> ServiceCall | None:
    """"打开/关闭"在六个 domain 下各自对应什么服务。"""
    domain = entity.get("domain", "")
    label = "打开" if on else "关闭"

    if domain in ("light", "switch", "input_boolean", "fan", "humidifier", "automation"):
        return _call(domain, "turn_on" if on else "turn_off", entity, {}, label)
    if domain in ("climate", "media_player", "script"):
        return _call(domain, "turn_on" if on else "turn_off", entity, {}, label)
    if domain == "cover":
        # 窗帘没有 turn_on，"打开窗帘"是 open_cover。
        return _call(domain, "open_cover" if on else "close_cover", entity, {}, label)
    if domain == "valve":
        return _call(domain, "open_valve" if on else "close_valve", entity, {}, label)
    if domain == "lock":
        # 门锁的"打开"是**解锁**（unlock），"关闭"才是上锁。这与其它 domain
        # 的 on=启用 正好相反，写反了就是"用户想开门，结果把门锁死了"。
        return _call(domain, "unlock" if on else "lock", entity, {}, "解锁" if on else "上锁")
    if domain == "scene":
        # 场景只能"激活"，没有"关闭场景"这回事。
        return _call(domain, "turn_on", entity, {}, "激活场景") if on else None
    return None


# ── 属性类 ──────────────────────────────────────────────────────────────
def prop_call(entity: dict[str, Any], cap: str, value: Any) -> ServiceCall | None:
    """属性设定。``value`` 已由校验层消歧/换算成最终目标值。"""
    domain = entity.get("domain", "")

    if cap == "brightness" and domain == "light":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        percent = max(0, min(100, round(float(value))))
        if percent <= 0:
            # HA 的 brightness_pct 是 1-100，0 会报错；语义上"亮度 0"就是关灯。
            return _call(domain, "turn_off", entity, {}, "关灯")
        return _call(domain, "turn_on", entity, {"brightness_pct": percent}, f"亮度 {percent}%")

    if cap == "color_temp" and domain == "light":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        kelvin = int(round(float(value)))
        return _call(domain, "turn_on", entity, {"color_temp_kelvin": kelvin}, f"色温 {kelvin}K")

    if cap == "color" and domain == "light":
        rgb = _to_rgb(value)
        if rgb is None:
            return None
        return _call(domain, "turn_on", entity, {"rgb_color": list(rgb)}, "颜色")

    if cap == "temperature" and domain == "climate":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        return _call(domain, "set_temperature", entity, {"temperature": round(float(value), 1)}, f"温度 {value}°C")

    if cap == "hvac_modes" and domain == "climate":
        # 选 off 时用 set_hvac_mode 而不是 turn_off：语义更明确，而且部分集成
        # 不实现 turn_off，只认 hvac_mode=off。
        return _call(domain, "set_hvac_mode", entity, {"hvac_mode": str(value)}, f"模式 {value}")

    if cap == "fan_modes" and domain == "climate":
        return _call(domain, "set_fan_mode", entity, {"fan_mode": str(value)}, f"风速 {value}")

    if cap == "swing_modes" and domain == "climate":
        return _call(domain, "set_swing_mode", entity, {"swing_mode": str(value)}, f"风向 {value}")

    if cap == "preset_modes":
        return _call(domain, "set_preset_mode", entity, {"preset_mode": str(value)}, f"预设 {value}")

    if cap == "position" and domain == "cover":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        position = max(0, min(100, round(float(value))))
        return _call(domain, "set_cover_position", entity, {"position": position}, f"开合度 {position}%")

    if cap == "volume" and domain == "media_player":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        percent = max(0, min(100, round(float(value))))
        return _call(domain, "volume_set", entity, {"volume_level": round(percent / 100, 3)}, f"音量 {percent}%")

    if cap == "source_list" and domain == "media_player":
        return _call(domain, "select_source", entity, {"source": str(value)}, f"音源 {value}")

    if cap == "effect_list" and domain == "light":
        return _call(domain, "turn_on", entity, {"effect": str(value)}, f"灯效 {value}")

    return None


def action_call(entity: dict[str, Any], verb: str) -> ServiceCall | None:
    """动作型指令（播放、暂停、开、关、停）。"""
    domain = entity.get("domain", "")

    if domain == "media_player":
        entry = MEDIA_SERVICES.get(verb)
        if entry is None:
            return None
        service, data = entry
        return _call(domain, service, entity, dict(data), verb)

    if domain == "cover":
        mapping = {"打开": "open_cover", "关闭": "close_cover", "停止": "stop_cover", "暂停": "stop_cover"}
        service = mapping.get(verb)
        return _call(domain, service, entity, {}, verb) if service else None

    if domain == "vacuum":
        mapping = {"开始": "start", "暂停": "pause", "停止": "stop", "回充": "return_to_base"}
        service = mapping.get(verb)
        return _call(domain, service, entity, {}, verb) if service else None

    if domain == "scene":
        return _call(domain, "turn_on", entity, {}, "激活场景")

    return None


def scene_call(entity: dict[str, Any]) -> ServiceCall:
    """场景/脚本激活。scene 与 script 的服务名都叫 turn_on，但归属不同 domain。"""
    domain = entity.get("domain") or "scene"
    return _call(domain, "turn_on", entity, {}, "激活场景")


def _to_rgb(value: Any) -> tuple[int, int, int] | None:
    """接受 int（0xRRGGBB）、"#RRGGBB"、[r,g,b] 或 (r,g,b)。"""
    if isinstance(value, (list, tuple)) and len(value) == 3:
        try:
            return tuple(max(0, min(255, int(part))) for part in value)  # type: ignore[return-value]
        except (TypeError, ValueError):
            return None
    if isinstance(value, int) and not isinstance(value, bool):
        return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if len(text) == 6:
            try:
                number = int(text, 16)
            except ValueError:
                return None
            return ((number >> 16) & 0xFF, (number >> 8) & 0xFF, number & 0xFF)
    return None
