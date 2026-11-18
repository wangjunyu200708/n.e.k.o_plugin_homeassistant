"""多指令拆分：一句话里含多个动作时，切成多条独立指令。

**来源**：逻辑复制自 `n.e.k.o_plugin_mijia/nlp/splitter.py`，仅替换
`LEAD_VERB_RE` 的动词表（米家的"回充/出舱/集尘/洗拖布"换成 HA 的
"播放/暂停/下一首/静音"等）。切分正则、填充词剥离、预检契约一律照搬。

边界：**只做文本切分，不做语义判断**。切出来的段能不能真的执行，由调用方
（插件的分发层）拿 `route()` 逐段预检来决定——任一段路由不了就整句回退，
这样单指令路径的行为与拆分功能上线前完全一致。

切分是"宁可切错也不漏切"的：切错只是白跑一遍路由预检然后回退，漏切则是
功能缺失。真正兜住误切的是调用方的预检，不是这里的正则。
"""

from __future__ import annotations

import re

from .normalizer import normalize_utterance

# 标点分隔：出现即切（"打开客厅灯，关掉插座"）
_PUNCT_SPLIT_RE = re.compile(r"[，,；;、]+")

# 连接词分隔。刻意不收单字"再"：它作程度副词时（"再高一点"）频率太高。
_CONN_SPLIT_RE = re.compile(r"(?:然后再|接着|然后|顺便|同时|并且|还有)")

# 并列连词。无条件切，靠预检兜底：设备名带"和"（"和风空调"）切坏后，
# 至少有一段路由不了，调用方会整句回退。
_CONJ_SPLIT_RE = re.compile(r"(?:以及|和|跟|与)")

# 句首控制动词：既用于判断某段是否自带动作，也用于给短句补共享动词
# （"打开客厅灯和卧室灯" → 第二段补成"打开卧室灯"）。
# 必须是捕获组：分发层要靠 group(1) 取出动词本身去补后段。
# 交替顺序按"长词优先、单字殿后"：把"开/关"放最后，否则"开始播放"会被
# 单字"开"截断，补共享动词时就变成"开播放"，反而拼出一条错指令。
LEAD_VERB_RE = re.compile(
    r"^(取消静音|返回充电|下一首|上一个|下一个|播放|暂停|继续|停止|静音|"
    r"打开|开启|关闭|关掉|开始|启动|回充|执行|运行|触发|开|关)"
)

# 拆分后每段首尾残留的副词/语气词。**不剥"了"**——"卧室电视关了"里的"了"
# 是动词的一部分，剥掉会改变原文语义。
_LEAD_FILLER_RE = re.compile(r"^(?:也|都|还|再|就|先|顺手|一起)+")
_TAIL_FILLER_RE = re.compile(r"(?:都|一起|吧|呢|啊)$")
# 段内的"也/都"紧邻动词时（"卧室灯也关掉"）会污染设备名。
_INNER_FILLER_RE = re.compile(r"(?:也|都)(?=(?:打开|开启|关闭|关掉|播放|暂停|停止|开|关))")


def _clean_segment(segment: str) -> str:
    """去掉切分后残留的礼貌前缀、句首副词与句尾语气词。"""
    s = normalize_utterance(segment.strip())
    s = _LEAD_FILLER_RE.sub("", s).strip()
    s = _INNER_FILLER_RE.sub("", s).strip()
    s = _TAIL_FILLER_RE.sub("", s).strip()
    return s


def _split_by_words(chunk: str) -> list[str]:
    """按连接词与并列连词切一段文本，保留顺序。"""
    parts: list[str] = []
    for piece in _CONN_SPLIT_RE.split(chunk):
        parts.extend(_CONJ_SPLIT_RE.split(piece))
    return parts


def split_commands(text: str) -> list[str]:
    """把一句多动作指令切成多条；单动作（或无有效切分）时原样返回 ``[text]``。

    返回的顺序与原文一致。**单段时返回的是原始文本**，不是归一化后的文本——
    调用方会拿段文本当 `raw_text`，单指令路径必须保持原文不变。
    """
    raw = (text or "").strip()
    if not raw:
        return [raw]

    norm = normalize_utterance(raw)
    segments: list[str] = []
    for chunk in _PUNCT_SPLIT_RE.split(norm):
        segments.extend(_split_by_words(chunk))

    cleaned = [s for s in (_clean_segment(seg) for seg in segments) if s]
    if len(cleaned) < 2:
        return [raw]
    return cleaned


def inherit_verb(segment: str, verb: str) -> str:
    """给不含动作动词的短句补上共享动词（'卧室灯' + '打开' → '打开卧室灯'）。

    ``verb`` 为空或该段已自带动词时原样返回。
    """
    if not verb or not segment or LEAD_VERB_RE.match(segment):
        return segment
    return f"{verb}{segment}"
