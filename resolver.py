"""目标解析：把 LLM 给的 targets 字符串匹配到具体实体。

**只做确定性匹配，不做语义推测。** 语义理解由调用方（NEKO 的 LLM）负责，
这一层只回答"这个名字指的是哪个实体"，并且宁可返回"找不到/有歧义"也不猜。

匹配优先级（逐级短路，命中即停）：

    1. entity_id 精确匹配          light.ceiling_2
    2. 名字 / 别名 / 设备名精确匹配  客厅吸顶灯
    3. "区域 + 名字" 拼接后精确匹配  客厅吸顶灯（区域名来自 HA 区域设置）
    4. 区域名                      客厅 → 该区域下的实体
    5. 子串双向包含                吸顶灯 → 客厅吸顶灯

**任何一级命中多个都报 ambiguous，由 LLM 回头问用户，插件不替用户挑。**

第 4 级刻意**不**自动展开成"该区域全部设备"。原因：客厅里可能有灯、音箱和
窗帘，一句"开客厅"若展开成 turn_on，会把窗帘一起打开（cover 的 turn_on 就是
open_cover）。想批量控制就让 LLM 先 find_devices(area=..., domain=...) 拿到
明确清单，再逐台下发——多一次往返，换掉一次不可逆的误动作。

名字归一化只做**大小写折叠 + 去空白**，不做同义词/繁简/分词——那是 NLP，
已经不在本插件的职责范围内。用户想用别的叫法，用别名（aliases.json）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 匹配结果状态。
OK = "ok"
AMBIGUOUS = "ambiguous"
NOT_FOUND = "not_found"

# 单个目标回溯时最多列几个候选，避免把整个实体表塞进对话历史。
MAX_CANDIDATES = 5


def _norm(text: Any) -> str:
    """归一化：去空白 + 大小写折叠。只做这些，别加别的。"""
    return "".join(str(text or "").split()).lower()


def _alias_list(entity: dict[str, Any]) -> list[str]:
    """实体上的别名，逗号分隔（HA 注册表别名 + 插件别名）。"""
    raw = entity.get("alias") or ""
    return [part for part in str(raw).split(",") if part.strip()]


@dataclass
class TargetMatch:
    """一个 target 字符串的解析结果。"""

    target: str
    status: str
    entity: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)

    def public_entity(self, *, fields: tuple[str, ...] = ("entity_id", "name", "area_name", "domain", "state")) -> dict:
        """给 LLM 看的实体摘要——只留定位与判断所需的字段。"""
        if not self.entity:
            return {}
        return {key: self.entity.get(key, "") for key in fields}

    def candidate_briefs(self) -> list[dict[str, Any]]:
        return [
            {
                "entity_id": item.get("entity_id", ""),
                "name": item.get("name", ""),
                "area_name": item.get("area_name", ""),
                "domain": item.get("domain", ""),
            }
            for item in self.candidates[:MAX_CANDIDATES]
        ]


class EntityIndex:
    """实体索引。一次解析建一次，避免对每个 target 重复线性扫描。"""

    def __init__(self, entities: list[dict[str, Any]]) -> None:
        self.entities = entities
        self._by_id: dict[str, dict[str, Any]] = {}
        self._by_name: dict[str, list[dict[str, Any]]] = {}
        self._by_area: dict[str, list[dict[str, Any]]] = {}

        for entity in entities:
            entity_id = str(entity.get("entity_id") or "")
            if not entity_id:
                continue
            self._by_id[_norm(entity_id)] = entity

            keys = {_norm(entity.get("name")), _norm(entity.get("device_name"))}
            keys.update(_norm(alias) for alias in _alias_list(entity))
            # "区域 + 名字" 作为一种额外的精确键，覆盖用户直接说全名的情况。
            area = _norm(entity.get("area_name"))
            if area:
                keys.add(area + _norm(entity.get("name")))
            keys.discard("")
            for key in keys:
                self._by_name.setdefault(key, []).append(entity)

            if area:
                self._by_area.setdefault(area, []).append(entity)

    def resolve(self, target: str, *, domain: str = "") -> TargetMatch:
        """解析单个目标。``domain`` 只用于缩小歧义候选，不改变匹配顺序。"""
        raw = str(target or "").strip()
        if not raw:
            return TargetMatch(raw, NOT_FOUND)
        key = _norm(raw)

        # 1. entity_id
        hit = self._by_id.get(key)
        if hit is not None:
            return TargetMatch(raw, OK, hit)

        # 2/3. 名字 / 别名 / 设备名 / 区域+名字 精确
        exact = self._narrow(self._by_name.get(key, []), domain)
        if len(exact) == 1:
            return TargetMatch(raw, OK, exact[0])
        if len(exact) > 1:
            return self._pick(raw, exact)

        # 4. 区域名（必须早于子串匹配：否则"客厅"会先命中"客厅吸顶灯"）
        area_hits = self._by_area.get(key, [])
        if domain:
            area_hits = [entity for entity in area_hits if entity.get("domain") == domain]
        if len(area_hits) == 1:
            return TargetMatch(raw, OK, area_hits[0])
        if len(area_hits) > 1:
            return self._pick(raw, area_hits)

        # 5. 子串双向包含
        contains = [
            entity
            for entity in self.entities
            if self._contains(entity, key) and (not domain or entity.get("domain") == domain)
        ]
        exact_contains = self._narrow(contains, domain)
        if len(exact_contains) == 1:
            return TargetMatch(raw, OK, exact_contains[0])
        if len(exact_contains) > 1:
            return self._pick(raw, exact_contains)

        return TargetMatch(raw, NOT_FOUND)

    def _contains(self, entity: dict[str, Any], key: str) -> bool:
        names = {_norm(entity.get("name")), _norm(entity.get("device_name"))}
        names.update(_norm(alias) for alias in _alias_list(entity))
        names.discard("")
        return any(key in name or name in key for name in names)

    @staticmethod
    def _narrow(candidates: list[dict[str, Any]], domain: str) -> list[dict[str, Any]]:
        if not domain:
            return candidates
        filtered = [entity for entity in candidates if entity.get("domain") == domain]
        return filtered or candidates

    @staticmethod
    def _pick(target: str, candidates: list[dict[str, Any]]) -> TargetMatch:
        """同名多实体：优先挑用户手里唯一可控的那个，否则报歧义。

        "唯一可控"指只有一个在线。这在"客厅灯泡坏了但没从 HA 删掉"的场景下
        很常见，直接报歧义会让用户困惑。
        """
        online = [entity for entity in candidates if entity.get("is_online", True)]
        if len(online) == 1:
            return TargetMatch(target, OK, online[0])
        return TargetMatch(target, AMBIGUOUS, None, candidates)


def resolve_targets(
    targets: list[str],
    entities: list[dict[str, Any]],
    *,
    domain: str = "",
) -> list[TargetMatch]:
    """批量解析。重复 target 只解析一次，但保留原始顺序。"""
    index = EntityIndex(entities)
    cache: dict[str, TargetMatch] = {}
    out: list[TargetMatch] = []
    for target in targets:
        key = str(target or "").strip()
        if key not in cache:
            cache[key] = index.resolve(key, domain=domain)
        out.append(cache[key])
    return out


def expand_targets(matches: list[TargetMatch]) -> list[dict[str, Any]]:
    """把解析结果摊平成去重后的实体列表（区域命中会展开成多台）。"""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for match in matches:
        if match.status != OK:
            continue
        group = match.candidates or ([match.entity] if match.entity else [])
        for entity in group:
            entity_id = str(entity.get("entity_id") or "")
            if entity_id and entity_id not in seen:
                seen.add(entity_id)
                out.append(entity)
    return out
