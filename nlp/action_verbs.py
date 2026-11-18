"""动作动词：媒体类、窗帘类、扫地机类的"非开关"指令。

**与 mijia 的关键差异**：米家的动作是设备 spec 里的 `actions`（siid/aiid），
需要 `VERB_TO_ACTION` 把中文动词映射成动作名；HA 的动作就是 `domain.service`，
所以这里**不需要映射表**——动词原样传给 `ha_api/services.py::action_call()`，
由它按 domain 选服务。

刻意不收"打开/关闭"：它们在 HA 里对每种 domain 有不同最优解（灯是 turn_on、
窗帘是 open_cover、车库门要二次确认），统一由 `power_call()` 处理，避免两条
路径给出不同结果。
"""

import re
from typing import Optional

ACTION_VERBS = (
    r"取消静音|继续播放|上一首|上一曲|上一个|下一首|下一曲|下一个|"
    r"播放|暂停|停止|静音|快进|快退|回充|返回充电|开始"
)

# 动词在尾部："音箱播放" → (音箱, 播放)
_VERB_LAST_RE = re.compile(r"(.+?)(?:的|把|让)?\s*(" + ACTION_VERBS + r")(?:吧|呢|啊)?$")
# 动词在开头："播放音乐" → (音乐, 播放)
_VERB_FIRST_RE = re.compile(r"(" + ACTION_VERBS + r")\s*(.+)")


def match_action_verb(command: str) -> Optional[tuple[str, str]]:
    """匹配动作动词，返回 (device_hint, verb)；未命中返回 None。

    动词在尾部优先（"客厅音箱播放"），其次动词开头（"播放客厅"）。
    两种词序下设备名为空都视为未命中——"播放"单独一句没有目标对象，
    应当落到 unknown 分支让用户补充，而不是猜一台音箱。
    """
    cmd = command.strip()
    m = _VERB_LAST_RE.match(cmd)
    if m:
        device_hint = (m.group(1) or "").strip()
        verb = (m.group(2) or "").strip()
        if device_hint and verb:
            return device_hint, verb
    m2 = _VERB_FIRST_RE.match(cmd)
    if m2:
        verb = (m2.group(1) or "").strip()
        device_hint = (m2.group(2) or "").strip()
        if device_hint and verb:
            return device_hint, verb
    return None
