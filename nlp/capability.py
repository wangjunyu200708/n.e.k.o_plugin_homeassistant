"""能力层：属性/值/单位校验 + CommandIR + 属性消歧。

**来源**：骨架来自 `n.e.k.o_plugin_mijia/nlp/capability.py`，但校验对象从
米家 spec 的 `value_range` / `value_list` 换成 HA 归一化后的 `caps`。

这一层同时是**「中文属性 ↔ caps 键」的唯一真源**。`ha_api/services.py`
不再自己维护映射表，而是接收本层算好的 cap 名与已消歧的取值——否则"模式"
在 climate/light/media_player 下的三种含义会在两个文件里各判一次，迟早分叉。

边界：NLP 只产生"明确实体 + 明确属性 + 值 + 单位"；本层依据实体能力判断
该组合是否成立，不做语义候选（"把那个弄亮点"这类归 LLM 前端）。
"""

from dataclasses import dataclass, field
from typing import Any, Optional

# 中文属性 → caps 键。长词与短词并列时以本表的键为准（PROP_TERMS 负责分界）。
PROP_TO_CAP = {
    "亮度": "brightness",
    "色温": "color_temp",
    "颜色": "color",
    "色彩": "color",
    "模式": "hvac_modes",
    "温度": "temperature",
    "风速": "fan_modes",
    "档位": "fan_modes",
    "风向": "swing_modes",
    "预设": "preset_modes",
    "位置": "position",
    "开合度": "position",
    "开度": "position",
    "音量": "volume",
    "音源": "source_list",
    "输入源": "source_list",
    "效果": "effect_list",
}

# caps 键 → 语义单位
_CAP_UNIT = {
    "brightness": "percent",
    "volume": "percent",
    "position": "percent",
    "color_temp": "kelvin",
    "temperature": "celsius",
    "color": "color",
    "hvac_modes": "enum",
    "fan_modes": "enum",
    "swing_modes": "enum",
    "preset_modes": "enum",
    "source_list": "enum",
    "effect_list": "enum",
    "media_actions": "enum",
}

# 命令携带的单位 → 语义单位
_COMMAND_UNIT = {
    "度": "celsius",
    "℃": "celsius",
    "°": "celsius",
    "%": "percent",
    "档": "level",
    "档位": "level",
    "K": "kelvin",
    "k": "kelvin",
    "开尔文": "kelvin",
}

# 语义单位 → 可接受的字面单位（写进错误 detail，供模型自纠）
_UNITS_FOR_SEMANTIC = {
    "celsius": ["度", "℃", "°"],
    "percent": ["%"],
    "level": ["档", "档位"],
    "kelvin": ["K", "开尔文"],
    "color": [],
    "enum": [],
}

# 枚举取值的同义词。各集成给的选项五花八门（low / medium / middle / quiet），
# 用户说的是中文，只能双向模糊匹配。这是**校验与执行共用的唯一一份**。
ENUM_SYNONYMS: dict[str, list[str]] = {
    "low": ["低", "小", "弱", "低档", "低速", "1档"],
    "medium": ["中", "中等", "中档", "中速", "2档", "middle"],
    "high": ["高", "大", "强", "高档", "高速", "3档"],
    "auto": ["自动", "auto"],
    "silent": ["静音", "安静", "quiet"],
    "off": ["关", "关闭", "停止"],
    "on": ["开", "开启"],
    "cool": ["制冷", "冷", "冷风"],
    "heat": ["制热", "热", "暖", "暖风"],
    "heat_cool": ["制冷制热", "冷暖", "自动冷热"],
    "fan_only": ["送风", "风扇", "吹风"],
    "dry": ["除湿", "干燥"],
}


@dataclass
class CommandIR:
    """指令的中间表示——前端与后端的接缝。

    与米家版的两处差异：
      * 删掉 ``gang`` / ``require_explicit_gang``：HA 里多控开关天生是多个
        独立实体，不存在"同一设备的第几路"概念。
      * 加 ``area`` 与 ``scene_keywords``：HA 的区域是实体属性而非命名约定，
        场景靠关键词匹配实体。

    ``action`` 取**语义动作**（switch / set_prop / adjust_prop / scene /
    query / action），不是路由分支名。
    """

    raw_text: str = ""
    action: str = ""
    area: str = ""
    device: str = ""
    entity_id: str = ""
    entity_info: Optional[dict] = None
    attribute: str = ""
    value: Any = None
    unit: str = ""
    direction: int = 1
    delta: Optional[float] = None
    verb: str = ""
    match_status: str = ""
    match_message: str = ""
    candidates: list = field(default_factory=list)
    scene_keywords: list = field(default_factory=list)


@dataclass
class ValidationResult:
    """能力校验结果。

    status: "valid" | "invalid_unit" | "invalid_value" | "unknown_attribute"
            | "enum_value_invalid"
    resolved: 枚举消歧后的官方取值（"高" → "high"）。执行层直接用这个值，
              不再二次匹配，保证校验与执行对同一个取值达成一致。
    detail: 结构化补充（允许范围 / 期望单位 / 可用属性），供错误 detail 透出。
    """

    status: str
    message: str = ""
    detail: dict = field(default_factory=dict)
    resolved: Any = None


def command_unit(unit: str) -> Optional[str]:
    """命令单位 → 语义单位（度→celsius、%→percent、K→kelvin）。"""
    return _COMMAND_UNIT.get(unit)


def cap_for_prop(entity: dict, prop: str) -> str:
    """中文属性 → 该实体上的 caps 键（按 domain 消歧）。

    "模式"是唯一的歧义词：climate 指 HVAC 模式，light 指灯效，
    media_player 指输入源。其余属性一对一。
    """
    domain = entity.get("domain", "")
    caps = entity.get("caps") or {}
    if prop == "模式":
        if domain == "climate":
            return "hvac_modes"
        if domain == "media_player":
            return "source_list"
        if domain == "light" and caps.get("effect_list"):
            return "effect_list"
        return "hvac_modes"
    return PROP_TO_CAP.get(prop, "")


def available_props(entity: dict) -> list[str]:
    """该实体支持的中文属性列表（错误 detail 用）。"""
    caps = entity.get("caps") or {}
    out: list[str] = []
    for prop, cap in PROP_TO_CAP.items():
        if prop in ("档位", "开度", "色彩", "输入源"):
            continue  # 只列主词，避免 detail 里一堆同义词
        if cap == "color" and not caps.get("color"):
            continue
        if cap and cap in caps:
            out.append(prop)
        elif cap == "position" and "position" in caps:
            out.append(prop)
    return out


def resolve_percent_prop(entity: dict, prop: str) -> str:
    """裸 "50%" 的属性消歧。

    `infer_prop_from_unit` 把裸百分比一律推成"亮度"，但 HA 里同样的 50%
    可能是音量或窗帘开合度。实体没有 brightness 时按 音量 → 开合度 顺延；
    都有时保持"亮度"（灯的绝对主导场景）。
    """
    if prop != "亮度":
        return prop
    caps = entity.get("caps") or {}
    if "brightness" in caps:
        return "亮度"
    if "volume" in caps:
        return "音量"
    if "position" in caps:
        return "开合度"
    return prop


def prop_bounds(entity: dict, cap: str) -> list:
    """caps 里的范围 → [min, max, step]，与 `resolve_adjust_target` 对齐。"""
    spec = (entity.get("caps") or {}).get(cap)
    if not isinstance(spec, dict):
        return []
    bounds = [spec.get("min", 0), spec.get("max", 100)]
    if "step" in spec:
        bounds.append(spec["step"])
    return bounds


def current_value(entity: dict, cap: str) -> Optional[float]:
    """取当前值用于相对调整（"调暗一点"要先知道现在多亮）。

    量纲一律换成与用户语言一致的那套：亮度百分比、音量百分比、开合度百分比。
    """
    attrs = entity.get("attributes") or {}

    if cap == "brightness":
        raw = attrs.get("brightness")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return round(float(raw) * 100 / 255)
        return None
    if cap == "volume":
        raw = attrs.get("volume_level")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return round(float(raw) * 100)
        return None
    if cap == "position":
        raw = attrs.get("current_position")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return float(raw)
        return None
    if cap == "temperature":
        raw = attrs.get("temperature")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return float(raw)
        return None
    if cap == "color_temp":
        kelvin = attrs.get("color_temp_kelvin")
        if isinstance(kelvin, (int, float)) and not isinstance(kelvin, bool):
            return float(kelvin)
        mireds = attrs.get("color_temp")
        if isinstance(mireds, (int, float)) and not isinstance(mireds, bool) and mireds > 0:
            return round(1_000_000 / float(mireds))
        return None
    return None


def match_option(options: list, wanted: Any) -> Optional[str]:
    """在设备给出的枚举里找到用户说的那一个。

    三级：精确（忽略大小写）→ 同义词 → 子串。找不到返回 None，由调用方把
    ``options`` 原样放进错误 detail。
    """
    if not options or wanted is None:
        return None
    target = str(wanted).strip().lower()
    if not target:
        return None

    for option in options:
        if str(option).lower() == target:
            return str(option)

    for option in options:
        for word in ENUM_SYNONYMS.get(str(option).lower(), []):
            if word.lower() == target or word.lower() in target:
                return str(option)

    for option in options:
        if target in str(option).lower() or str(option).lower() in target:
            return str(option)
    return None


def validate_command(entity: dict, attribute: str, value: Any, unit: str = "") -> ValidationResult:
    """校验属性/值/单位与该实体能力是否兼容。

    失败时附带结构化 ``detail``：模型拿到「允许的范围 / 期望的单位 / 可用属性」
    才能自行修正重试，而不是只看到一句散文。
    """
    cap = cap_for_prop(entity, attribute)
    caps = entity.get("caps") or {}
    props = available_props(entity)

    if not cap or cap not in caps:
        return ValidationResult(
            "unknown_attribute",
            f"'{entity.get('name', '该设备')}'没有'{attribute}'这个属性",
            {"attribute": attribute, "available": props},
        )

    expected_unit = _CAP_UNIT.get(cap, "")

    # ── 枚举型：值必须能对上设备的某一项 ──
    if expected_unit == "enum":
        options = caps.get(cap) or []
        matched = match_option(options, value)
        if matched is None:
            return ValidationResult(
                "enum_value_invalid",
                f"'{attribute}'不支持'{value}'。可选：{', '.join(str(o) for o in options)}",
                {"attribute": attribute, "value": value, "allowed": options},
            )
        return ValidationResult("valid", resolved=matched)

    # ── 颜色：交给执行层解析 RGB，这里只确认能力存在 ──
    if expected_unit == "color":
        return ValidationResult("valid", resolved=value)

    # ── 范围型：先校验单位，再校验数值 ──
    given = command_unit(unit)
    if given and given != expected_unit:
        # "档"用在亮度上是最常见的错法，给出期望单位让模型自纠。
        return ValidationResult(
            "invalid_unit",
            f"'{attribute}'不接受单位'{unit}'，应使用 {_UNITS_FOR_SEMANTIC.get(expected_unit, [])}",
            {
                "attribute": attribute,
                "unit_given": unit,
                "expected_unit": expected_unit,
                "allowed_units": _UNITS_FOR_SEMANTIC.get(expected_unit, []),
            },
        )

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        bounds = prop_bounds(entity, cap)
        if len(bounds) >= 2 and (value < bounds[0] or value > bounds[1]):
            return ValidationResult(
                "invalid_value",
                f"'{attribute}'={value} 超出设备支持范围 {bounds[0]}~{bounds[1]}",
                {
                    "attribute": attribute,
                    "value": value,
                    "min": bounds[0],
                    "max": bounds[1],
                    "step": bounds[2] if len(bounds) >= 3 else None,
                },
            )
    return ValidationResult("valid", resolved=value)


def to_command_ir(result, *, raw_text: Optional[str] = None) -> CommandIR:
    """把路由结果展平为 CommandIR（鸭子类型，避免循环导入）。

    ``action`` 取语义动作：有 parsed 时用 parsed.action，否则退回分支名。
    """
    branch = getattr(result, "branch", "")
    text = raw_text if raw_text is not None else getattr(result, "raw_text", "")
    ir = CommandIR(raw_text=text, action=branch)
    ir.scene_keywords = list(getattr(result, "scene_keywords", None) or [])

    match = getattr(result, "match", None)
    if match is not None:
        ir.match_status = match.status
        ir.match_message = match.message or ""
        ir.candidates = list(getattr(match, "candidates", None) or [])
        if match.status == "ok" and match.device is not None:
            ir.entity_info = match.device
            ir.area = match.device.get("area_name", "")
            ir.device = match.device.get("name", "")
            ir.entity_id = match.device.get("entity_id", "")
    if not ir.device:
        ir.device = getattr(result, "device_hint", "")

    ir.verb = getattr(result, "verb", "") or ""
    parsed = getattr(result, "parsed", None)
    if parsed is not None:
        ir.action = getattr(parsed, "action", "") or branch
        ir.attribute = parsed.prop or ""
        ir.value = parsed.value
        ir.unit = parsed.unit
        ir.direction = parsed.direction
        ir.delta = parsed.delta
    elif branch == "switch":
        ir.action = "switch"
        ir.attribute = "开关"
    return ir
