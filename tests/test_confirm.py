"""安全层单测：白名单分档、确认令牌生命周期、审计日志脱敏。

插件层（`control_devices`）的集成行为在 `test_llm_tool_contract.py` 里测，
这里只测 `security.py` 自己的逻辑——两边都测是因为"规则对不对"和"规则有没有
被正确调用"是两类完全不同的 bug。
"""

import time

import pytest
from _fixtures import make_alarm, make_garage, make_light, make_lock, make_sensor, make_switch, make_water_heater
from security import (
    REASON_AUTO_LOW,
    REASON_AUTO_MID,
    REASON_HIGH_NEEDS_APPROVAL,
    REASON_UNSUPPORTED,
    REASON_USER_DISABLED,
    REASON_USER_ENABLED,
    AuditLog,
    ConfirmStore,
    Whitelist,
)


class TestWhitelistDecision:
    def test_low_risk_allowed_by_default(self):
        allowed, reason = Whitelist().decision(make_light())
        assert allowed is True
        assert reason == REASON_AUTO_LOW

    def test_mid_risk_allowed_by_default(self):
        allowed, reason = Whitelist().decision(make_switch())
        assert allowed is True
        assert reason == REASON_AUTO_MID

    @pytest.mark.parametrize(
        "factory",
        [make_lock, make_garage, make_water_heater, make_alarm],
    )
    def test_high_risk_blocked_by_default(self, factory):
        allowed, reason = Whitelist().decision(factory())
        assert allowed is False
        assert reason == REASON_HIGH_NEEDS_APPROVAL

    def test_read_only_domain_blocked(self):
        allowed, reason = Whitelist().decision(make_sensor())
        assert allowed is False
        assert reason == REASON_UNSUPPORTED

    def test_unknown_domain_never_falls_through_to_low(self):
        # 未知 domain 的 risk_of 会返回 low，必须先按 is_supported 拦下。
        unknown = {"entity_id": "vacuum.x", "domain": "vacuum", "risk": "low"}
        allowed, reason = Whitelist().decision(unknown)
        assert allowed is False
        assert reason == REASON_UNSUPPORTED

    def test_user_override_beats_risk(self):
        whitelist = Whitelist()
        whitelist.set_entity("lock.front_door", True)
        allowed, reason = whitelist.decision(make_lock())
        assert allowed is True
        assert reason == REASON_USER_ENABLED

    def test_override_cannot_resurrect_unsupported_domain(self):
        # 覆盖表只是 entity_id → bool，它不知道类型；给传感器勾"允许"不该
        # 让它变成可控设备。
        whitelist = Whitelist()
        whitelist.set_entity("sensor.living_room_temperature", True)
        allowed, reason = whitelist.decision(make_sensor())
        assert allowed is False
        assert reason == REASON_UNSUPPORTED

    def test_user_disable_beats_low_risk(self):
        whitelist = Whitelist()
        whitelist.set_entity("light.living_room_ceiling", False)
        allowed, reason = whitelist.decision(make_light())
        assert allowed is False
        assert reason == REASON_USER_DISABLED

    def test_clear_override_restores_auto(self):
        whitelist = Whitelist()
        whitelist.set_entity("lock.front_door", True)
        whitelist.clear_override("lock.front_door")
        assert whitelist.allows(make_lock()) is False

    def test_filter(self):
        entities = [make_light(), make_lock(), make_switch()]
        kept = Whitelist().filter(entities)
        assert {entity["entity_id"] for entity in kept} == {"light.living_room_ceiling", "switch.study_plug"}

    def test_empty_entity_id_rejected(self):
        with pytest.raises(ValueError):
            Whitelist().set_entity("  ", True)


class TestWhitelistBulk:
    def test_bulk_skips_high_risk(self):
        whitelist = Whitelist()
        entities = [make_light(), make_lock(), make_garage()]
        changed = whitelist.set_entities_allowed(entities, True)
        # 只改到灯；门锁与车库门即使 bulk 允许也不会被写入。
        assert changed == 1
        assert "lock.front_door" not in whitelist.overrides

    def test_domain_bulk_skips_high_risk(self):
        whitelist = Whitelist()
        entities = [make_lock()]
        assert whitelist.set_domain_allowed(entities, "lock", True) == 0
        assert whitelist.allows(make_lock()) is False

    def test_domain_bulk_works_for_low_risk(self):
        whitelist = Whitelist()
        assert whitelist.set_domain_allowed([make_light()], "light", False) == 1
        assert whitelist.allows(make_light()) is False


class TestWhitelistPersistence:
    def test_roundtrip(self):
        whitelist = Whitelist()
        whitelist.set_entity("lock.front_door", True)
        whitelist.set_entity("light.a", False)
        restored = Whitelist.from_dict(whitelist.to_dict())
        assert restored.overrides == {"lock.front_door": True, "light.a": False}

    @pytest.mark.parametrize("payload", [None, {}, {"overrides": []}, {"overrides": "x"}, 42])
    def test_bad_payload_falls_back_to_empty(self, payload):
        assert Whitelist.from_dict(payload).overrides == {}


class TestConfirmStore:
    def test_issue_returns_token(self):
        store = ConfirmStore()
        pending = store.issue(["lock.front_door"], "unlock")
        assert len(pending.token) == 8
        assert pending.summary["action"] == "unlock"

    def test_consume_ok(self):
        store = ConfirmStore()
        pending = store.issue(["lock.front_door"], "unlock")
        assert store.consume(pending.token, ["lock.front_door"], "unlock") == "ok"

    def test_consume_is_single_use(self):
        store = ConfirmStore()
        pending = store.issue(["lock.front_door"], "unlock")
        store.consume(pending.token, ["lock.front_door"], "unlock")
        assert store.consume(pending.token, ["lock.front_door"], "unlock") == "no_pending"

    def test_no_pending(self):
        assert ConfirmStore().consume("deadbeef", ["lock.front_door"], "unlock") == "no_pending"

    def test_token_mismatch(self):
        store = ConfirmStore()
        store.issue(["lock.front_door"], "unlock")
        assert store.consume("00000000", ["lock.front_door"], "unlock") == "token_mismatch"

    def test_fingerprint_changes_with_target(self):
        store = ConfirmStore()
        pending = store.issue(["lock.front_door"], "unlock")
        assert store.consume(pending.token, ["lock.back_door"], "unlock") == "fingerprint_mismatch"

    def test_fingerprint_changes_with_params(self):
        store = ConfirmStore()
        pending = store.issue(["valve.gas"], "set", {"position": 50})
        assert store.consume(pending.token, ["valve.gas"], "set", {"position": 100}) == "fingerprint_mismatch"

    def test_fingerprint_ignores_entity_order(self):
        store = ConfirmStore()
        pending = store.issue(["lock.a", "lock.b"], "unlock")
        assert store.consume(pending.token, ["lock.b", "lock.a"], "unlock") == "ok"

    def test_expired(self):
        store = ConfirmStore(ttl=-1)
        pending = store.issue(["lock.front_door"], "unlock")
        assert store.consume(pending.token, ["lock.front_door"], "unlock") == "expired"

    def test_only_latest_pending_kept(self):
        store = ConfirmStore()
        first = store.issue(["lock.front_door"], "unlock")
        store.issue(["lock.front_door"], "unlock")
        assert store.consume(first.token, ["lock.front_door"], "unlock") == "token_mismatch"

    def test_peek_prunes_expired(self):
        store = ConfirmStore(ttl=-1)
        store.issue(["lock.front_door"], "unlock")
        assert store.peek() is None

    def test_clear(self):
        store = ConfirmStore()
        store.issue(["lock.front_door"], "unlock")
        store.clear()
        assert store.peek() is None

    def test_public_hides_fingerprint(self):
        store = ConfirmStore()
        public = store.issue(["lock.front_door"], "unlock").public()
        assert "fingerprint" not in public
        assert public["expires_in"] > 0
        assert public["targets"] == ["lock.front_door"]


class TestAuditLog:
    def test_appends_jsonl(self, tmp_path):
        log = AuditLog(tmp_path / "audit.jsonl")
        log.record(action="turn_on", targets=["light.a"], result="ok", risk="low")
        text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
        assert '"turn_on"' in text
        assert '"light.a"' in text

    def test_scrubs_jwt_like_detail(self, tmp_path):
        log = AuditLog(tmp_path / "audit.jsonl")
        log.record(action="x", targets=["a"], result="ok", detail="token=eyJhbGciOiJIUzI1NiJ9abcdefg")
        text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
        assert "eyJhbGci" not in text
        assert "redacted-jwt" in text

    def test_scrubs_long_hex(self, tmp_path):
        log = AuditLog(tmp_path / "audit.jsonl")
        log.record(action="x", targets=["a"], result="ok", detail="a" * 40)
        assert "redacted-hex" in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")

    def test_truncates_targets(self, tmp_path):
        log = AuditLog(tmp_path / "audit.jsonl")
        log.record(action="x", targets=[f"e{i}" for i in range(50)], result="ok")
        text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
        assert "e19" in text
        assert "e20" not in text

    def test_stops_growing_past_limit(self, tmp_path):
        log = AuditLog(tmp_path / "audit.jsonl")
        log.MAX_BYTES = 10
        log.record(action="x", targets=["a"], result="ok")
        size_after_first = (tmp_path / "audit.jsonl").stat().st_size
        log.record(action="y", targets=["b"], result="ok")
        assert (tmp_path / "audit.jsonl").stat().st_size == size_after_first

    def test_write_failure_is_swallowed(self, tmp_path):
        # 审计写不进去不该把控制流程带崩。
        log = AuditLog(tmp_path / "no_such_dir" / "audit.jsonl")
        log.record(action="x", targets=["a"], result="ok")

    def test_expiry_window_is_two_minutes(self):
        from security import CONFIRM_TTL

        assert 60 <= CONFIRM_TTL <= 300
        assert time.time() > 0
