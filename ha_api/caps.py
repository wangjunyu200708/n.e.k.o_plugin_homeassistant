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
HAZARD_COVER_CLASSES = {"garage", "garage_door", "gate", "door"}

# ── 风险分级 ────────────────────────────────────────────────────────────
# 三档，决定白名单默认值与是否需要两步确认。分档依据是"误操作会不会让人或
# 财物暴露在物理风险里"，不是"操作是否可逆"：
#
#   low   —— 灯、风扇、影音、场景、普通窗帘。开错了只是不舒服。
#   mid   —— 开关/插座。本身无害，但插座后面可能接着电暖器、充电器。
#   high  —— 锁、报警面板、车库门/大门/栅栏门、阀门（燃气/水）、热水器。
RISK_LOW = "low"
RISK_MID = "mid"
RISK_HIGH = "high"

LOW_RISK_DOMAINS = {"light", "fan", "media_player", "scene", "script", "input_boolean"}
MID_RISK_DOMAINS = {"switch", "climate"}
HIGH_RISK_DOMAINS = {"lock", "alarm_control_panel", "valve", "water_heater"}
# cover 按 device_class 二选一，见 risk_of()。

# domain → 危险服务（两步确认的判定依据）。
# 只收"让物理世界暴露"的方向：开、解锁、撤防、加热。关/上锁一律放行。
_DANGEROUS_SERVICES = {
    "lock": ("unlock", "open"),
    "alarm_control_panel": ("alarm_disarm",),
    "valve": ("open_valve",),
    "water_heater": ("turn_on", "set_temperature"),
}

# alarm_control_panel 的 supported_features 位掩码 → 可用武装模式。
ALARM_ARM_HOME = 1
ALARM_ARM_AWAY = 2
ALARM_ARM_NIGHT = 4
ALARM_ARM_CUSTOM_BYPASS = 16
ALARM_ARM_VACATION = 32

# 首版明确不支持的 domain（给用户一个清楚的说法，而不是"设备没找到"）。
UNSUPPORTED_DOMAINS = {
    "vacuum": "扫地机",
    "humidifier": "加湿器",
    "camera": "摄像头",
    "sensor": "传感器（只读）",
    "binary_sensor": "传感器（只读）",
    "weather": "天气",
    "person": "人员定位",
    "device_tracker": "设备追踪",
}

# 可控制（可写）的 domain。白名单只在这个集合里生成。
CONTROLLABLE_DOMAINS = {
    "light",
    "switch",
    "input_boolean",
    "fan",
    "cover",
    "climate",
    "media_player",
    "scene",
    "script",
    "lock",
    "alarm_control_panel",
    "valve",
    "water_heater",
}

# 只读但允许查询的 domain。
READ_ONLY_DOMAINS = {"sensor", "binary_sensor", "weather", "person", "device_tracker", "sun"}


def risk_of(domain: str, device_class: str = "") -> str:
    """domain + device_class → 风险档位。

    cover 是唯一需要看 device_class 的 domain：普通窗帘低风险，车库门/大门/
    栅栏门/门高风险。其余 domain 按类型一刀切。
    """
    if domain == "cover":
        return RISK_HIGH if str(device_class or "").lower() in HAZARD_COVER_CLASSES else RISK_LOW
    if domain in HIGH_RISK_DOMAINS:
        return RISK_HIGH
    if domain in MID_RISK_DOMAINS:
        return RISK_MID
    return RISK_LOW



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
        if device_class in HAZARD_COVER_CLASSES:
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
        caps["power"] = False
        features = _int(attrs, "supported_features")
        modes: list[str] = []
        for bit, name in (
            (ALARM_ARM_HOME, "home"),
            (ALARM_ARM_AWAY, "away"),
            (ALARM_ARM_NIGHT, "night"),
            (ALARM_ARM_CUSTOM_BYPASS, "custom"),
            (ALARM_ARM_VACATION, "vacation"),
        ):
            if features & bit:
                modes.append(name)
        # supported_features 缺失时（部分集成不上报）给出三种最常用的模式，
        # 让"撤防"这个高危动作仍然可达；实际调用失败会明确报回。
        caps["arm_modes"] = modes or ["home", "away", "night"]

    elif domain == "fan":
        caps["power"] = True
        if "percentage" in attrs or "percentage_step" in attrs:
            caps["percentage"] = _range(0, 100, _num(attrs, "percentage_step") or 1)
        presets = _str_list(attrs, "preset_modes")
        if presets:
            caps["preset_modes"] = presets
        if "oscillating" in attrs:
            caps["oscillate"] = True

    elif domain == "valve":
        caps["power"] = True  # 语义上是 open_valve / close_valve
        if "current_position" in attrs:
            caps["position"] = _range(0, 100)

    elif domain == "water_heater":
        caps["power"] = True
        temp_min = _num(attrs, "min_temp")
        temp_max = _num(attrs, "max_temp")
        if temp_min is not None and temp_max is not None:
            caps["temperature"] = _range(temp_min, temp_max, _num(attrs, "target_temp_step") or 0.5)
        ops = _str_list(attrs, "operation_list")
        if ops:
            caps["operation_list"] = ops

    elif domain == "script":
        caps["power"] = True  # turn_on = 运行脚本

    if domain in _DANGEROUS_SERVICES and "dangerous" not in caps:
        caps["dangerous"] = list(_DANGEROUS_SERVICES[domain])

    # 风险档位写进 caps，白名单与工具返回值都读它，避免两处各判一次。
    caps["risk"] = risk_of(domain, str(attrs.get("device_class") or ""))

    return caps


def is_supported(domain: str) -> bool:
    """该 domain 是否在可控制范围内（白名单只从中生成）。"""
    return domain in CONTROLLABLE_DOMAINS


def is_read_only(domain: str) -> bool:
    return domain in READ_ONLY_DOMAINS


def caps_unit(caps: dict[str, Any], cap_name: str) -> str:
    """能力 → 语义单位（用于校验与错误 detail）。"""
    if cap_name in ("brightness", "volume", "position", "percentage"):
        return "percent"
    if cap_name == "color_temp":
        return "kelvin"
    if cap_name == "temperature":
        return "celsius"
    if cap_name in (
        "hvac_modes",
        "fan_modes",
        "preset_modes",
        "source_list",
        "effect_list",
        "media_actions",
        "arm_modes",
        "operation_list",
    ):
        return "enum"
    return ""
