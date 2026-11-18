"""意图路由：smart_control 的 5 步短路。

**来源**：骨架复制自 `n.e.k.o_plugin_mijia/nlp/router.py`，改动：

  * 场景分支返回**关键词列表**而不是固定场景名——HA 的场景是用户自建的
    `scene.xxx` 实体，各家叫法不同，只能拿关键词去匹配实体名。
  * 去掉房间映射参数：HA 的区域名在拉取实体时已合并进 ``area_name``，
    不需要运行期再调 API 补。
  * 动作分支的"设备匹配成功才提交"判据保留——它防的是"烘干机温度50度"
    被拆成动作+设备这种经典误判，HA 侧同样存在（"播放器音量30"）。
"""

from dataclasses import dataclass, field
from typing import Optional

from . import intent_terms as T
from .action_verbs import match_action_verb
from .control_parser import ParseResult, parse_control_command
from .device_matcher import MatchResult, match_devices
from .normalizer import normalize_utterance


@dataclass
class RouteResult:
    """路由结果，branch 取值：

    - "scene":   场景/脚本激活（scene_keywords 为候选关键词）
    - "query":   状态查询（device_hint）
    - "action":  动作型指令（device_hint + verb + match）
    - "switch":  二元开关（parsed.action == "switch" + match）
    - "control": 属性/模式控制（parsed + match）
    - "unknown": 无法理解
    """

    branch: str
    device_hint: str = ""
    scene_keywords: list = field(default_factory=list)
    verb: str = ""
    query_prop: str = ""
    match: Optional[MatchResult] = None
    parsed: Optional[ParseResult] = None
    message: str = ""
    raw_text: str = ""

    def __repr__(self) -> str:
        return f"RouteResult(branch={self.branch!r}, device_hint={self.device_hint!r})"


async def route(utterance: str, devices: list) -> RouteResult:
    """5 步短路路由：场景 → 开关标记 → 查询 → 动作动词 → 属性控制。"""
    command = normalize_utterance(utterance if isinstance(utterance, str) else "")
    raw_text = utterance if isinstance(utterance, str) else ""

    # === 场景/脚本 ===
    scene_keywords = T.detect_scene(command)
    if scene_keywords:
        return RouteResult(branch="scene", scene_keywords=scene_keywords, raw_text=raw_text)

    # === 开关指令标记（最高优先级，防止动作动词分支抢先匹配）===
    is_switch_cmd = bool(T.SWITCH_CMD_RE.match(command)) and not T.ACTION_VERB_PREFIX_RE.match(command)

    # === 查询意图（开关指令优先，防止"关闭卧室灯怎么样"被查询分支劫持）===
    query_match = T.QUERY_RE.search(command)
    if not is_switch_cmd and query_match:
        device_hint = command[: query_match.start()].strip()
        # 剥掉属性名后缀，剥出的属性词作为 query_prop
        prop_match = T.QUERY_PROP_SUFFIX_RE.search(device_hint)
        query_prop = ""
        if prop_match:
            query_prop = prop_match.group(0)
            device_hint = T.QUERY_PROP_SUFFIX_RE.sub("", device_hint).strip()
        if device_hint:
            return RouteResult(
                branch="query",
                device_hint=device_hint,
                query_prop=query_prop or T.QUERY_KEYWORD_PROP_MAP.get(query_match.group(0), ""),
                raw_text=raw_text,
            )

    # === 动作型指令（播放/暂停/下一首等），开关指令跳过 ===
    if not is_switch_cmd:
        action = match_action_verb(command)
        if action is not None:
            device_hint, verb = action
            match = match_devices(device_hint, devices)
            # 只有唯一命中才提交 action 分支；否则回落 control/switch 解析，
            # 避免设备名以动作动词开头时被动作分支抢先导致识别失败
            if match.status == "ok" and len(match.devices) == 1:
                return RouteResult(
                    branch="action", device_hint=device_hint, verb=verb, match=match, raw_text=raw_text
                )

    # === 属性/模式控制 ===
    parsed = parse_control_command(command)
    if parsed is None:
        return RouteResult(branch="unknown", raw_text=raw_text)
    branch = "switch" if parsed.action == "switch" else "control"
    match = match_devices(parsed.device, devices)
    return RouteResult(branch=branch, device_hint=parsed.device, parsed=parsed, match=match, raw_text=raw_text)
