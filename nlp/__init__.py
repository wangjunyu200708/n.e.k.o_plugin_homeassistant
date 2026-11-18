"""NLP 规则引擎子包：意图路由 / 控制解析 / 实体匹配 / 数值解析。

全部为纯函数模块（无 IO、无网络、无第三方依赖），供 `HomeAssistantPlugin`
的 smart_control / query 入口调用。

与 `n.e.k.o_plugin_mijia/nlp/` 的关系：`normalizer` / `chinese_number` 逐字
复制，`splitter` / `value_resolver` / `control_parser` / `device_matcher` /
`capability` / `router` 保留骨架并换词表与数据模型，`action_verbs` 重写，
米家的 `positions`（多控方位）在 HA 下不存在，已删除。
"""

from .capability import (
    CommandIR,
    ValidationResult,
    available_props,
    cap_for_prop,
    command_unit,
    current_value,
    match_option,
    prop_bounds,
    resolve_percent_prop,
    to_command_ir,
    validate_command,
)
from .chinese_number import chinese_to_int
from .control_parser import ParseResult, parse_control_command
from .device_matcher import (
    MatchResult,
    find_scene,
    format_ambiguous_message,
    match_devices,
    summarize_candidates,
)
from .normalizer import normalize_utterance
from .router import RouteResult, route
from .value_resolver import (
    extract_number,
    infer_prop_from_unit,
    parse_delta,
    parse_prop_value,
    resolve_adjust_target,
)

__all__ = [
    "CommandIR",
    "MatchResult",
    "ParseResult",
    "RouteResult",
    "ValidationResult",
    "available_props",
    "cap_for_prop",
    "chinese_to_int",
    "command_unit",
    "current_value",
    "extract_number",
    "find_scene",
    "format_ambiguous_message",
    "infer_prop_from_unit",
    "match_devices",
    "match_option",
    "normalize_utterance",
    "parse_control_command",
    "parse_delta",
    "parse_prop_value",
    "prop_bounds",
    "resolve_adjust_target",
    "resolve_percent_prop",
    "route",
    "summarize_candidates",
    "to_command_ir",
    "validate_command",
]
