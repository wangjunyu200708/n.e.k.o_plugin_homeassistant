"""数值抽取 + 相对量计算 + 钳值。

**来源**：逻辑复制自 `n.e.k.o_plugin_mijia/nlp/value_resolver.py`，改为引用
本包的 `intent_terms`，并新增开尔文（K）与"百分比量纲"的单位支持。

HA 侧多了一个米家没有的单位：色温用开尔文（"色温调到 3000K"）。其余（度/℃/
%/档）语义一致。

`infer_prop_from_unit` 在 HA 下仍有歧义：裸"50%"可能是亮度，也可能是音量或
窗帘开合度。这里保持"默认亮度"以维持确定性，真正的消歧由
`capability.resolve_percent_prop()` 结合实体 caps 完成。
"""

import re
from typing import Any, Optional

from .chinese_number import chinese_to_int
from .intent_terms import PROP_TERMS, SPLIT_VERBS

# 中文数字（排除量词："一点/一些/一下"里的"一"不是数值）
_CN_NUM = r"[零〇一二两三四五六七八九十百千]+(?!点|些|下)"
# 数字 + 可选单位。K 必须独立成字符，避免与设备名里的英文字母粘连。
_NUMBER_WITH_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(%|度|℃|°|档|档位|[Kk])?")
# 中文数字 + 可选单位
_CN_NUM_UNIT_RE = re.compile(r"(" + _CN_NUM + r")\s*(%|度|℃|°|档|档位|[Kk])?")
# 相对量 delta 提取
_DELTA_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(%|度|℃|[Kk])?")
_CN_DELTA_RE = re.compile(r"(" + _CN_NUM + r")\s*(%|度|℃|[Kk])?")
# 中文后紧跟数字的分界
CN_DIGIT_BOUNDARY_RE = re.compile(r"(?<=[一-鿿])(\d)")

# 开尔文：只接受合理区间，防止 "3000" 被误当色温以外的量。判定交给调用方。
KELVIN_MIN = 1000
KELVIN_MAX = 10000


def extract_number(text: str) -> Optional[tuple[Any, str]]:
    """从文本中抽取第一个 (数值, 单位)；支持阿拉伯数字与中文数字。"""
    m = _NUMBER_WITH_UNIT_RE.search(text)
    if m:
        num_str = m.group(1)
        unit = m.group(2) or ""
        value = float(num_str) if "." in num_str else int(num_str)
        return value, unit
    m = _CN_NUM_UNIT_RE.search(text)
    if m:
        n = chinese_to_int(m.group(1))
        if n is not None:
            return n, m.group(2) or ""
    return None


def infer_prop_from_unit(unit: str, value: Any) -> Optional[str]:
    """无属性名时按单位推断属性。

    度/℃/° → 温度；K → 色温；档/档位 → 风速（HA 的档位几乎都是空调风速）。
    % 或 0-100 的裸整数 → 亮度（默认，可由 caps 修正）。
    """
    if unit in ("度", "℃", "°"):
        return "温度"
    if unit in ("K", "k"):
        return "色温"
    if unit in ("档", "档位"):
        return "风速"
    if unit == "%" or (isinstance(value, int) and 0 <= value <= 100):
        return "亮度"
    return None


def parse_prop_value(intent: str) -> Optional[tuple[Optional[str], Any, str]]:
    """解析"属性+数值"意图，返回 (prop_name, value, unit)。"""
    m = re.search(
        r"(?:" + SPLIT_VERBS + r")?\s*(" + PROP_TERMS + r")?"
        r"(\d+(?:\.\d+)?|" + _CN_NUM + r")\s*(%|度|℃|°|档|档位|[Kk])?",
        intent,
    )
    if not m:
        return None
    prop_name = m.group(1)
    num_str = m.group(2)
    unit = m.group(3) or ""
    if num_str.isdigit():
        value = float(num_str) if "." in num_str else int(num_str)
    else:
        n = chinese_to_int(num_str)
        if n is None:
            return None
        value = n
    return prop_name, value, unit


def parse_delta(text: str) -> Optional[float]:
    """从文本中提取相对调整的 delta 数值；支持中文数字（"调低两度"→2）。"""
    m = _DELTA_RE.search(text)
    if m:
        return float(m.group(1))
    m = _CN_DELTA_RE.search(text)
    if m:
        n = chinese_to_int(m.group(1))
        if n is not None:
            return float(n)
    return None


def resolve_adjust_target(
    cur_value: float, direction: int, delta: Optional[float], value_range: list
) -> float:
    """计算相对调整的目标值。

    - 显式 delta：cur + direction * delta
    - 无 delta：默认步长为 max(step, 范围*10%)
    - 结果 clamp 到 [v_min, v_max]，并按 step 对齐（step>1 时）

    HA 侧 ``value_range`` 由 `caps` 归一化而来，已是 [min, max, step] 形态，
    与米家的 `value_range` 语义一致。
    """
    v_min = value_range[0] if len(value_range) >= 1 else 0
    v_max = value_range[1] if len(value_range) >= 2 else 100
    step = value_range[2] if len(value_range) >= 3 else 1

    if delta is not None:
        target = cur_value + direction * delta
    else:
        range_size = v_max - v_min
        default_step = max(step, range_size * 0.1)
        target = cur_value + direction * default_step

    target = max(v_min, min(v_max, target))
    # step != 1 就要对齐：温度常见 step=0.5，对齐到 26.5 而不是 26.3，
    # 否则会把 0.3 这种空调根本设不进去的值发出去。
    if step and step != 1:
        target = round((target - v_min) / step) * step + v_min
        # 对齐可能越界（如 [0,100,60] 下 t=100 → round(100/60)*60=120），重新钳回
        target = max(v_min, min(v_max, target))
        # 消掉 0.5 步长带来的浮点尾巴（26.500000000000004）
        target = round(target, 4)
    return target
