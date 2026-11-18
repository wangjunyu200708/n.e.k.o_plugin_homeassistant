"""能力归一化：把 HA 的 domain + attributes 压成一张统一能力表。

为什么要有这一层：HA **没有**统一的设备能力表，能力只能从 domain 和
attributes 推断，而且同一个语义概念在不同 domain 下字段名与量纲都不一样：

    亮度    light            brightness          0-255
    色温    light            color_temp_kelvin   2000-6500
    温度    climate          temperature         7-35, step 0.5
    音量    media_player     volume_level        0.0-1.0
    开合度  cover            current_position    0-100

NLP 层和校验层若直接面对这些差异，每加一个 domain 都要改三处词表。所以这里
统一成 `caps`（capabilities）：

    {"power": True,
     "brightness": {"min": 1, "max": 100},
     "color_temp": {"min": 2000, "max": 6500},
     "temperature": {"min": 7, "max": 35, "step": 0.5},
     "hvac_modes": ["off", "cool", "heat"],
     "position": {"min": 0, "max": 100},
     "volume": {"min": 0, "max": 100},
     "source_list": [...],
     "dangerous": ["unlock"]}

约定：**所有百分比量纲统一为 0-100**（HA 内部的 0-255 与 0.0-1.0 由
`services.py` 在调用时换算），NLP 与校验只看这一套。

只写有值的键——缓存进 JSON 时要控体积，`None` 一律省略。
"""

from __future__ import annotations

from typing import Any

# ── supported_features 位掩码（只收本插件用得到的位）──
COVER_OPEN = 1
COVER_CLOSE = 2
COVER_SET_POSITION = 4
COVER_STOP = 8

MP_PAUSE = 1
MP_VOLUME_SET = 4
MP_VOLUME_MUTE = 8
MP_PREVIOUS_TRACK = 16
MP_NEXT_TRACK = 32
MP_TURN_ON = 128
MP_TURN_OFF = 256
MP_VOLUME_STEP = 1024
MP_SELECT_SOURCE = 2048
MP_STOP = 4096
MP_PLAY = 16384

# 可调亮度的 color_mode。`onoff` 是唯一不可调的取值。
_BRIGHTNESS_MODES = {"brightness", "color_temp", "hs", "xy", "rgb", "rgbw", "rgbww", "white"}
_COLOR_MODES = {"hs", "xy", "rgb", "rgbw", "rgbww"}

# 开合即"让物理世界暴露"的 cover 类型。窗帘/遮阳帘不在此列——开窗帘没有风险。
_HAZARD_COVER_CLASSES = {"garage", "garage_door", "gate", "door"}

# domain → 危险服务（两步确认的判定依据）。
_DANGEROUS_SERVICES = {
    "lock": ("unlock",),
    "alarm_control_panel": ("alarm_disarm",),
}

# 首版明确不支持的 domain（给用户一个清楚的说法，而不是"设备没找到"）。
UNSUPPORTED_DOMAINS = {
    "vacuum": "扫地机",
    "fan": "风扇",
    "humidifier": "加湿器",
    "water_heater": "热水器",
    "camera": "摄像头",
    "sensor": "传感器（只读）",
    "binary_sensor": "传感器（只读）",
    "weather": "天气",
    "person": "人员定位",
    "device_tracker": "设备追踪",
}

# 只读但允许查询的 domain。
READ_ONLY_DOMAINS = {"sensor", "binary_sensor", "weather", "person", "device_tracker", "sun"}


def _num(attrs: dict[str, Any], key: str) -> float | None:
    """取数值属性；HA 偶尔把数值序列化成字符串，或给 None。"""
    value = attrs.get(key)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _int(attrs: dict[str, Any], key: str) -> int:
    value = attrs.get(key)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _str_list(attrs: dict[str, Any], key: str) -> list[str]:
    value = attrs.get(key)
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if isinstance(item, (str, int, float))]


def _range(min_v: float, max_v: float, step: float | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"min": min_v, "max": max_v}
    if step is not None:
        out["step"] = step
    return out


def build_caps(domain: str, attrs: dict[str, Any]) -> dict[str, Any]:
    """按 domain 生成归一化能力表。未知 domain 返回空表（只读）。"""
    caps: dict[str, Any] = {}
    if domain in ("light", "switch", "input_boolean"):
        caps["power"] = True

    if domain == "light":
        modes = {m.lower() for m in _str_list(attrs, "supported_color_modes")}
        # supported_color_modes 缺失时（老集成）退化为"有 brightness 属性即可调"。
        if not modes or modes - {"onoff"}:
            caps["brightness"] = _range(1, 100)
        if modes & _COLOR_MODES:
            caps["color"] = True
        temp_min = _num(attrs, "min_color_temp_kelvin")
        temp_max = _num(attrs, "max_color_temp_kelvin")
        if temp_min is not None and temp_max is not None:
            caps["color_temp"] = _range(temp_min, temp_max)
        effects = _str_list(attrs, "effect_list")
        if effects:
            caps["effect_list"] = effects

    elif domain == "climate":
        caps["power"] = True
        hvac = _str_list(attrs, "hvac_modes")
        if hvac:
            caps["hvac_modes"] = hvac
        temp_min = _num(attrs, "min_temp")
        temp_max = _num(attrs, "max_temp")
        if temp_min is not None and temp_max is not None:
            caps["temperature"] = _range(temp_min, temp_max, _num(attrs, "target_temp_step") or 0.5)
        fans = _str_list(attrs, "fan_modes")
        if fans:
            caps["fan_modes"] = fans
        presets = _str_list(attrs, "preset_modes")
        if presets:
            caps["preset_modes"] = presets
        swings = _str_list(attrs, "swing_modes")
        if swings:
            caps["swing_modes"] = swings

    elif domain == "cover":
        features = _int(attrs, "supported_features")
        if features & COVER_SET_POSITION or "current_position" in attrs:
            caps["position"] = _range(0, 100)
        caps["power"] = True  # 语义上是 open/close，不是 turn_on
        device_class = str(attrs.get("device_class") or "").lower()
        caps["device_class"] = device_class
        if device_class in _HAZARD_COVER_CLASSES:
            caps["dangerous"] = ["open_cover"]

    elif domain == "media_player":
        features = _int(attrs, "supported_features")
        caps["power"] = True
        if features & MP_VOLUME_SET or "volume_level" in attrs:
            caps["volume"] = _range(0, 100)
        sources = _str_list(attrs, "source_list")
        if sources:
            caps["source_list"] = sources
        actions: list[str] = []
        for bit, name in (
            (MP_PLAY, "play"),
            (MP_PAUSE, "pause"),
            (MP_STOP, "stop"),
            (MP_NEXT_TRACK, "next"),
            (MP_PREVIOUS_TRACK, "previous"),
        ):
            if features & bit:
                actions.append(name)
        if actions:
            caps["media_actions"] = actions
        if features & MP_VOLUME_MUTE or "is_volume_muted" in attrs:
            caps["mute"] = True

    elif domain == "lock":
        caps["power"] = True  # 语义上是 lock/unlock
        caps["dangerous"] = ["unlock"]

    elif domain == "alarm_control_panel":
        caps["power"] = True
        caps["dangerous"] = ["alarm_disarm"]

    if domain in _DANGEROUS_SERVICES and "dangerous" not in caps:
        caps["dangerous"] = list(_DANGEROUS_SERVICES[domain])

    return caps


def is_supported(domain: str) -> bool:
    """该 domain 是否在首版可控制范围内。"""
    return domain not in UNSUPPORTED_DOMAINS


def is_read_only(domain: str) -> bool:
    return domain in READ_ONLY_DOMAINS


def caps_unit(caps: dict[str, Any], cap_name: str) -> str:
    """能力 → 语义单位（用于校验与错误 detail）。"""
    if cap_name in ("brightness", "volume", "position"):
        return "percent"
    if cap_name == "color_temp":
        return "kelvin"
    if cap_name == "temperature":
        return "celsius"
    if cap_name in ("hvac_modes", "fan_modes", "preset_modes", "source_list", "effect_list", "media_actions"):
        return "enum"
    return ""
