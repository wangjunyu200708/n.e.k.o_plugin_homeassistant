"""语义动作 → HA 服务调用。

这一层是 HA 专有知识的唯一落点：服务名、字段名、量纲换算、危险方向判定。
上层（工具层）只说 `action` + `params`，不碰 HA 细节。

## 动作词表

刻意用**固定英文枚举**而不是中文动词：这是给 LLM 的 function-calling 参数，
枚举越窄越不会选错，也不需要插件再猜同义词。

    turn_on / turn_off / toggle          灯、开关、风扇、空调、影音
    open / close / stop                  窗帘、阀门、门锁（open=解锁）
    set                                  属性设定，参数在 params 里
    play / pause / next / previous       影音播放控制
    mute / unmute                        静音
    lock / unlock                        门锁（**不用 turn_on 表达解锁**）
    arm_home / arm_away / arm_night      报警面板布防
    disarm                               报警面板撤防（危险方向）
    run                                  场景 / 脚本

`open` 用在 `lock` 上是解锁（危险），所以对锁请用 `lock`/`unlock` 这两个无歧义
的动作；`open` 给锁只在用户明确说"开门"时由上层映射，且同样判危险。

## 量纲换算

    上下文字段        单位              换算
    brightness_pct    light             0-100 → 直接
    color_temp_kelvin light             开尔文 → 直接
    temperature       climate/water     摄氏度 → climate 保留 1 位小数
    position          cover/valve       0-100 → 直接
    percentage        fan               0-100 → 直接
    volume_level      media_player      0-100 → 除以 100，保留 3 位

**0 亮度的语义是关灯**：HA 的 `brightness_pct` 合法区间是 1-100，传 0 会报错，
所以这里改发 `turn_off`。

## 危险方向

只判"让物理世界暴露"的方向：解锁、开高风险门、开阀门、撤防、给热水器加热。
关、上锁、布防一律放行——它们让世界更安全，弹确认只会训练用户无脑点确认。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .caps import HAZARD_COVER_CLASSES

# 给 LLM 的 action 枚举。顺序无关，但保持稳定便于阅读日志。
ACTIONS = (
    "turn_on",
    "turn_off",
    "toggle",
    "open",
    "close",
    "stop",
    "set",
    "play",
    "pause",
    "next",
    "previous",
    "mute",
    "unmute",
    "lock",
    "unlock",
    "arm_home",
    "arm_away",
    "arm_night",
    "disarm",
    "run",
)

# 动作 → 该 domain 下的 HA 服务名。值为 None 表示该 domain 不支持此动作。
_DOMAIN_ACTIONS: dict[str, dict[str, str]] = {
    "light": {
        "turn_on": "turn_on",
        "turn_off": "turn_off",
        "toggle": "toggle",
    },
    "switch": {
        "turn_on": "turn_on",
        "turn_off": "turn_off",
        "toggle": "toggle",
    },
    "input_boolean": {
        "turn_on": "turn_on",
        "turn_off": "turn_off",
        "toggle": "toggle",
    },
    "fan": {
        "turn_on": "turn_on",
        "turn_off": "turn_off",
        "toggle": "toggle",
    },
    "climate": {
        "turn_on": "turn_on",
        "turn_off": "turn_off",
        "toggle": "toggle",
    },
    "media_player": {
        "turn_on": "turn_on",
        "turn_off": "turn_off",
        "toggle": "toggle",
        "play": "media_play",
        "pause": "media_pause",
        "next": "media_next_track",
        "previous": "media_previous_track",
        "stop": "media_stop",
        "mute": "volume_mute",
        "unmute": "volume_mute",
    },
    "cover": {
        "open": "open_cover",
        "close": "close_cover",
        "stop": "stop_cover",
        "turn_on": "open_cover",
        "turn_off": "close_cover",
    },
    "valve": {
        "open": "open_valve",
        "close": "close_valve",
        "turn_on": "open_valve",
        "turn_off": "close_valve",
    },
    "lock": {
        "lock": "lock",
        "unlock": "unlock",
        "open": "open",
        "close": "lock",
    },
    "alarm_control_panel": {
        "arm_home": "alarm_arm_home",
        "arm_away": "alarm_arm_away",
        "arm_night": "alarm_arm_night",
        "disarm": "alarm_disarm",
    },
    "water_heater": {
        "turn_on": "turn_on",
        "turn_off": "turn_off",
    },
    "scene": {"run": "turn_on", "turn_on": "turn_on"},
    "script": {"run": "turn_on", "turn_on": "turn_on", "turn_off": "turn_off"},
}

# 危险方向：命中即需要两步确认。键是 service_id。
DANGEROUS_SERVICES = {
    ("lock", "unlock"),
    ("lock", "open"),
    ("alarm_control_panel", "alarm_disarm"),
    ("valve", "open_valve"),
    ("water_heater", "turn_on"),
    ("water_heater", "set_temperature"),
}

# params 里允许出现的键 → 该键要求的能力。用于"实体不支持这个参数"的明确报错。
PARAM_CAPS = {
    "brightness": "brightness",
    "color_temp": "color_temp",
    "color": "color",
    "temperature": "temperature",
    "hvac_mode": "hvac_modes",
    "fan_speed": "fan_modes",
    "swing_mode": "swing_modes",
    "preset": "preset_modes",
    "position": "position",
    "volume": "volume",
    "source": "source_list",
    "effect": "effect_list",
    "percentage": "percentage",
    "operation": "operation_list",
}


@dataclass
class ServiceCall:
    """一次待执行的 HA 服务调用。"""

    domain: str
    service: str
    data: dict[str, Any] = field(default_factory=dict)
    dangerous: bool = False
    action: str = ""

    @property
    def service_id(self) -> str:
        return f"{self.domain}.{self.service}"

    def brief(self) -> str:
        """语言无关的一句话，给审计日志与确认提示用（不含令牌）。"""
        payload = {key: value for key, value in self.data.items() if key != "entity_id"}
        return f"{self.service_id} {payload}" if payload else self.service_id


def is_dangerous(domain: str, service: str, entity: dict[str, Any] | None = None) -> bool:
    """判定一次调用是否需要两步确认。

    四条来源：锁的 unlock/open、报警面板撤防、阀门 open_valve、热水器加热。
    再加一条按实体判的：车库门 / 大门 / 栅栏门的 open_cover。
    """
    if (domain, service) in DANGEROUS_SERVICES:
        return True
    if domain == "cover" and service == "open_cover":
        caps = (entity or {}).get("caps") or {}
        device_class = str(caps.get("device_class") or (entity or {}).get("device_class") or "").lower()
        return device_class in HAZARD_COVER_CLASSES
    return False


def _num_param(params: dict[str, Any], key: str) -> float | None:
    value = params.get(key)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _percent_param(params: dict[str, Any], key: str) -> int | None:
    value = _num_param(params, key)
    if value is None:
        return None
    return max(0, min(100, round(value)))


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


def _allowed(caps: dict[str, Any], name: str, domain_ok: bool) -> bool:
    """该参数能否落到这台设备上。

    ``caps`` 为空时退回按 domain 判（手工构造的实体、老缓存），否则**以 caps
    为准**：同一个 light domain 里，只有 onoff 的灯就是没有 brightness，给它
    设亮度必须报 unsupported 而不是让 HA 静默吞掉。
    """
    if not domain_ok:
        return False
    if not caps:
        return True
    return name in caps


def _set_fields(domain: str, params: dict[str, Any], caps: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[str], bool]:
    """把 params 翻成服务字段。

    返回 (字段, 不支持的参数名列表, 是否触发了危险方向)。
    `set` 与 `turn_on` 共用这一段：调亮度本质上是 `light.turn_on` + 字段。
    """
    caps = caps or {}
    fields: dict[str, Any] = {}
    unsupported: list[str] = []
    dangerous = False

    if "brightness" in params:
        percent = _percent_param(params, "brightness")
        if percent is None or not _allowed(caps, "brightness", domain == "light"):
            unsupported.append("brightness")
        elif percent > 0:
            # 0 亮度由 build_call 翻成 turn_off，这里不产出字段。
            fields["brightness_pct"] = percent

    if "color_temp" in params:
        value = _num_param(params, "color_temp")
        if value is None or not _allowed(caps, "color_temp", domain == "light"):
            unsupported.append("color_temp")
        else:
            fields["color_temp_kelvin"] = int(round(value))

    if "color" in params:
        rgb = _to_rgb(params.get("color"))
        if rgb is None or not _allowed(caps, "color", domain == "light"):
            unsupported.append("color")
        else:
            fields["rgb_color"] = list(rgb)

    if "effect" in params:
        if not _allowed(caps, "effect_list", domain == "light"):
            unsupported.append("effect")
        else:
            fields["effect"] = str(params["effect"])

    if "temperature" in params:
        value = _num_param(params, "temperature")
        if value is not None and _allowed(caps, "temperature", domain in ("climate", "water_heater")):
            fields["temperature"] = round(value, 1)
            if domain == "water_heater":
                # 给热水器设温 = 可能加热到烫伤温度，判危险。
                dangerous = True
        else:
            unsupported.append("temperature")

    if "hvac_mode" in params:
        if _allowed(caps, "hvac_modes", domain == "climate"):
            fields["hvac_mode"] = str(params["hvac_mode"])
        else:
            unsupported.append("hvac_mode")

    if "fan_speed" in params:
        if _allowed(caps, "fan_modes", domain == "climate"):
            fields["fan_mode"] = str(params["fan_speed"])
        else:
            unsupported.append("fan_speed")

    if "swing_mode" in params:
        if _allowed(caps, "swing_modes", domain == "climate"):
            fields["swing_mode"] = str(params["swing_mode"])
        else:
            unsupported.append("swing_mode")

    if "preset" in params:
        if _allowed(caps, "preset_modes", domain in ("climate", "fan")):
            fields["preset_mode"] = str(params["preset"])
        else:
            unsupported.append("preset")

    if "position" in params:
        percent = _percent_param(params, "position")
        if percent is not None and _allowed(caps, "position", domain in ("cover", "valve")):
            fields["position"] = percent
        else:
            unsupported.append("position")

    if "percentage" in params:
        percent = _percent_param(params, "percentage")
        if percent is not None and _allowed(caps, "percentage", domain == "fan"):
            fields["percentage"] = percent
        else:
            unsupported.append("percentage")

    if "volume" in params:
        percent = _percent_param(params, "volume")
        if percent is not None and _allowed(caps, "volume", domain == "media_player"):
            fields["volume_level"] = round(percent / 100, 3)
        else:
            unsupported.append("volume")

    if "source" in params:
        if _allowed(caps, "source_list", domain == "media_player"):
            fields["source"] = str(params["source"])
        else:
            unsupported.append("source")

    if "operation" in params:
        if _allowed(caps, "operation_list", domain == "water_heater"):
            fields["operation_mode"] = str(params["operation"])
        else:
            unsupported.append("operation")

    return fields, unsupported, dangerous


def build_call(
    entity: dict[str, Any],
    action: str,
    params: dict[str, Any] | None = None,
) -> tuple[ServiceCall | None, list[str]]:
    """把 (实体, 动作, 参数) 编成一次服务调用。

    返回 ``(call, unsupported_params)``：
      * ``call is None`` —— 该实体不支持这个动作（调用方要明确报 unsupported，
        不能静默当成成功）
      * ``unsupported_params`` 非空 —— 动作能执行，但部分参数用不上（例如给
        开关设亮度）。**不静默丢弃**，由调用方一并回给 LLM。
    """
    params = params if isinstance(params, dict) else {}
    domain = str(entity.get("domain") or "")
    if domain not in _DOMAIN_ACTIONS:
        return None, []

    fields, unsupported, dangerous = _set_fields(domain, params, entity.get("caps") or {})

    if action == "set":
        # 亮度 0 的语义就是关灯；HA 的 brightness_pct 合法区间是 1-100。
        if not fields and _percent_param(params, "brightness") == 0:
            return (
                ServiceCall(
                    domain=domain,
                    service="turn_off",
                    data={"entity_id": entity.get("entity_id", "")},
                    action="turn_off",
                ),
                unsupported,
            )
        service = _settings_service(domain, fields)
        if service is None:
            return None, unsupported
    else:
        service = _DOMAIN_ACTIONS[domain].get(action)
        if service is None:
            return None, []

    if action == "mute":
        fields["is_volume_muted"] = True
    elif action == "unmute":
        fields["is_volume_muted"] = False

    data = {"entity_id": entity.get("entity_id", "")}
    data.update(fields)

    return (
        ServiceCall(
            domain=domain,
            service=service,
            data=data,
            dangerous=dangerous or is_dangerous(domain, service, entity),
            action=action,
        ),
        unsupported,
    )


def _settings_service(domain: str, fields: dict[str, Any]) -> str | None:
    """`set` 动作在该 domain 下该用哪个服务。

    由**实际算出来的字段**决定，而不是由 params 的键决定——否则给空调只传
    fan_speed 时会误发 set_temperature 加一个空温度。
    """
    if domain == "light":
        return "turn_on"
    if domain == "fan":
        if "percentage" in fields:
            return "set_percentage"
        if "preset_mode" in fields:
            return "set_preset_mode"
        return "turn_on"
    if domain == "cover":
        return "set_cover_position" if "position" in fields else None
    if domain == "valve":
        return "set_valve_position" if "position" in fields else None
    if domain == "climate":
        for key, service in (
            ("temperature", "set_temperature"),
            ("hvac_mode", "set_hvac_mode"),
            ("fan_mode", "set_fan_mode"),
            ("swing_mode", "set_swing_mode"),
            ("preset_mode", "set_preset_mode"),
        ):
            if key in fields:
                return service
        return None
    if domain == "water_heater":
        if "temperature" in fields:
            return "set_temperature"
        if "operation_mode" in fields:
            return "set_operation_mode"
        return None
    if domain == "media_player":
        if "volume_level" in fields:
            return "volume_set"
        if "source" in fields:
            return "select_source"
        return None
    return None


def scene_call(entity: dict[str, Any]) -> ServiceCall:
    """场景/脚本激活。scene 与 script 的服务名都叫 turn_on，但归属不同 domain。"""
    domain = str(entity.get("domain") or "scene")
    return ServiceCall(domain, "turn_on", {"entity_id": entity.get("entity_id", "")}, False, "run")
