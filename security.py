"""安全层：可控实体白名单、两步确认令牌、审计日志。

三件事放在一起，因为它们共享同一套风险判断，拆开就会出现"白名单按 A 规则放行、
确认按 B 规则拦截"的错位。

## 白名单不是"用户勾出来的清单"

它是一条**规则 + 显式覆盖**：

    实体没有显式覆盖时 → 按 risk 自动决定
        low  → 允许（灯、风扇、影音、场景、普通窗帘）
        mid  → 允许（开关/插座/空调，面板上会标出来）
        high → **拒绝**（锁、报警面板、车库门/大门、阀门、热水器）
    实体有显式覆盖时 → 听用户的

这样新增设备自动归类：低风险的开箱即可用，高风险的永远进不来，除非用户
在面板上单独勾了那一个实体。**高危不给整类一键全开**——`set_domain_allowed`
对高危 domain 直接拒绝。

## 两步确认的威胁模型

**防误判，不防恶意。** 令牌通过工具返回值交给 LLM，再由 LLM 转述给用户。
宿主会把工具输出写进对话历史，所以 LLM 天然知道令牌，理论上可以不等用户
确认就自己重放。

能做的硬约束都做了：

    * 一次性消费
    * 绑定 (实体集合 + 动作 + 参数) 指纹，换目标即失效
    * 120 秒过期
    * 只保留最新一条（连说两次不会攒出两个待执行动作）

要彻底堵住这个口子，得让令牌**绕开 LLM** 走人的通道（例如面板上点确认），
那是另一套交互，不在本版范围内。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from .ha_api.caps import RISK_HIGH, RISK_LOW, RISK_MID, is_supported, risk_of

# 覆盖原因码，面板按它显示不同的解释文案。
REASON_USER_ENABLED = "user_enabled"
REASON_USER_DISABLED = "user_disabled"
REASON_AUTO_LOW = "auto_low"
REASON_AUTO_MID = "auto_mid"
REASON_HIGH_NEEDS_APPROVAL = "high_needs_approval"
REASON_UNSUPPORTED = "unsupported"

# 确认令牌有效期（秒）。太短来不及打字，太长等于没有。
CONFIRM_TTL = 120.0


@dataclass
class Whitelist:
    """可控实体白名单。``overrides`` 是用户显式决定，优先于自动分档。"""

    overrides: dict[str, bool] = field(default_factory=dict)

    # ── 判定 ────────────────────────────────────────────────────────────
    def decision(self, entity: dict[str, Any]) -> tuple[bool, str]:
        """返回 (是否允许, 原因码)。

        顺序很重要：**先挡 domain，再看用户覆盖**。原因是被覆盖表是一个纯
        entity_id → bool 的映射，它不知道实体的类型；若先看覆盖，给一个传感器
        或扫地机勾上「允许」就等于凭空造出一个"可控"的假象。
        """
        # 不在可控制范围内的 domain（扫地机、摄像头、传感器…）一律拒绝。
        if not is_supported(str(entity.get("domain") or "")):
            return False, REASON_UNSUPPORTED

        entity_id = str(entity.get("entity_id") or "")
        if entity_id and entity_id in self.overrides:
            allowed = bool(self.overrides[entity_id])
            return allowed, REASON_USER_ENABLED if allowed else REASON_USER_DISABLED

        risk = str(entity.get("risk") or risk_of(str(entity.get("domain") or ""), entity.get("device_class", "")))
        if risk == RISK_HIGH:
            return False, REASON_HIGH_NEEDS_APPROVAL
        if risk == RISK_MID:
            return True, REASON_AUTO_MID
        if risk == RISK_LOW:
            return True, REASON_AUTO_LOW
        return False, REASON_UNSUPPORTED

    def allows(self, entity: dict[str, Any]) -> bool:
        return self.decision(entity)[0]

    def filter(self, entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [entity for entity in entities if self.allows(entity)]

    # ── 修改 ────────────────────────────────────────────────────────────
    def set_entity(self, entity_id: str, allowed: bool) -> None:
        """显式设定单个实体。高危实体只能走这条路进来。"""
        key = str(entity_id or "").strip()
        if not key:
            raise ValueError("entity_id 不能为空")
        self.overrides[key] = bool(allowed)

    def clear_override(self, entity_id: str) -> None:
        self.overrides.pop(str(entity_id or "").strip(), None)

    def set_domain_allowed(self, entities: list[dict[str, Any]], domain: str, allowed: bool) -> int:
        """按 domain 整类放行/禁止。高危实体**自动跳过**，只能逐个开。

        这是"不给整类一键全开"的落点：面板上点"全选开关"能生效，点"全选
        门锁"只会改到那些本来就不高危的，门锁一个都不会被打开。
        """
        same_domain = [entity for entity in entities if entity.get("domain") == domain]
        return self.set_entities_allowed(same_domain, allowed)

    def set_entities_allowed(self, entities: list[dict[str, Any]], allowed: bool) -> int:
        """批量设定，但**自动跳过高危实体**。返回实际改动数量。"""
        changed = 0
        for entity in entities:
            if str(entity.get("risk") or "") == RISK_HIGH:
                continue
            entity_id = str(entity.get("entity_id") or "")
            if entity_id:
                self.overrides[entity_id] = bool(allowed)
                changed += 1
        return changed

    # ── 持久化 ──────────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        return {"version": 1, "overrides": dict(self.overrides)}

    @classmethod
    def from_dict(cls, data: Any) -> Whitelist:
        if not isinstance(data, dict):
            return cls()
        raw = data.get("overrides")
        if not isinstance(raw, dict):
            return cls()
        return cls(overrides={str(key): bool(value) for key, value in raw.items() if isinstance(key, str)})


def _fingerprint(entity_ids: list[str], action: str, params: dict[str, Any] | None) -> str:
    """待执行动作的指纹。换实体、换动作、换参数都会变。"""
    payload = json.dumps(
        {
            "entities": sorted(str(item) for item in entity_ids),
            "action": str(action or ""),
            "params": params or {},
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


@dataclass
class PendingConfirm:
    """一条待确认的高危动作。"""

    token: str
    fingerprint: str
    expires_at: float
    summary: dict[str, Any] = field(default_factory=dict)

    def expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at

    def public(self) -> dict[str, Any]:
        """给 LLM 的确认要求。不含指纹，避免它被当成可复用的凭据。"""
        return {
            "token": self.token,
            "expires_in": max(0, int(self.expires_at - time.time())),
            "action": self.summary.get("action", ""),
            "targets": self.summary.get("targets", []),
            "params": self.summary.get("params", {}),
        }


class ConfirmStore:
    """进程内的待确认动作表。只保留最新一条，一次性消费。"""

    def __init__(self, ttl: float = CONFIRM_TTL) -> None:
        self.ttl = ttl
        self._pending: PendingConfirm | None = None

    def issue(
        self,
        entity_ids: list[str],
        action: str,
        params: dict[str, Any] | None = None,
        *,
        targets: list[str] | None = None,
    ) -> PendingConfirm:
        token = secrets.token_hex(4)
        pending = PendingConfirm(
            token=token,
            fingerprint=_fingerprint(entity_ids, action, params),
            expires_at=time.time() + self.ttl,
            summary={
                "action": action,
                "params": params or {},
                "targets": targets or list(entity_ids),
                "entity_ids": list(entity_ids),
            },
        )
        # 只保留最新一条：连说两次"解锁大门"不应该攒出两个待执行动作。
        self._pending = pending
        return pending

    def consume(self, token: str, entity_ids: list[str], action: str, params: dict[str, Any] | None = None) -> str:
        """校验并消费令牌。

        返回原因码：``ok`` / ``no_pending`` / ``expired`` / ``token_mismatch``
        / ``fingerprint_mismatch``。除 ``ok`` 外都不执行。
        """
        pending = self._pending
        if pending is None:
            return "no_pending"
        if pending.expired():
            self._pending = None
            return "expired"
        if not token or not secrets.compare_digest(str(token).strip().lower(), pending.token):
            return "token_mismatch"
        if pending.fingerprint != _fingerprint(entity_ids, action, params):
            return "fingerprint_mismatch"
        self._pending = None
        return "ok"

    def peek(self) -> PendingConfirm | None:
        if self._pending and self._pending.expired():
            self._pending = None
        return self._pending

    def clear(self) -> None:
        self._pending = None


class AuditLog:
    """审计日志：记录谁在什么时候对什么设备做了什么。

    **绝不记录令牌、Authorization 头、完整 URL 查询串。** 日志会进
    `plugins/homeassistant/data/audit.jsonl`，用户可能随手贴出来求助。
    """

    MAX_BYTES = 1024 * 1024  # 1 MB 截断，避免无限增长

    def __init__(self, path: Any) -> None:
        self.path = path

    def record(
        self,
        *,
        action: str,
        targets: list[str],
        result: str,
        detail: str = "",
        risk: str = "",
    ) -> None:
        entry = {
            "ts": round(time.time(), 3),
            "action": str(action),
            "targets": [str(item) for item in targets][:20],
            "result": str(result),
            "risk": str(risk),
        }
        if detail:
            # 兜底：即便调用方传错，也不让长串令牌形状的东西进日志。
            entry["detail"] = _scrub(detail)[:300]
        try:
            if self.path.exists() and self.path.stat().st_size > self.MAX_BYTES:
                return
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            # 审计日志写不进去不该影响主流程。
            pass


def _scrub(text: str) -> str:
    """把看起来像 JWT/长十六进制串的片段替换掉。"""
    import re

    text = re.sub(r"eyJ[A-Za-z0-9_\-\.]{10,}", "<redacted-jwt>", text)
    text = re.sub(r"\b[0-9a-f]{32,}\b", "<redacted-hex>", text)
    return text
