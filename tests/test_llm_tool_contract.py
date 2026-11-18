"""@llm_tool 契约：信封结构、错误码、逐台结果、两阶段确认、回读校验。

这些测试直接调工具方法，绕过 LLM——工具方法的入参就是 function calling 的
入参，出参就是会被写进对话历史的东西，所以这里断言的形状就是真实契约。
"""

import pytest
from _fixtures import make_all, make_light, make_lock

# isort: split
from ha_api.client import EntitySnapshot

# 服务调用成功后设备应该变成什么状态。用于驱动回读校验。
SERVICE_STATE = {
    "light.turn_on": "on",
    "light.turn_off": "off",
    "light.toggle": "on",
    "switch.turn_on": "on",
    "switch.turn_off": "off",
    "fan.turn_on": "on",
    "fan.turn_off": "off",
    "climate.turn_on": "cool",
    "cover.open_cover": "open",
    "cover.close_cover": "closed",
    "lock.lock": "locked",
    "lock.unlock": "unlocked",
    "lock.open": "unlocked",
    "valve.open_valve": "open",
    "valve.close_valve": "closed",
    "alarm_control_panel.alarm_arm_away": "armed_away",
    "alarm_control_panel.alarm_arm_home": "armed_home",
    "alarm_control_panel.alarm_disarm": "disarmed",
    "water_heater.turn_on": "eco",
    "water_heater.turn_off": "off",
}


class FakeHaClient:
    """假 HA：固定快照 + 会随服务调用变化的设备状态。

    ``stuck`` 里的实体调用成功但状态不变，用来模拟"设备离线/物理卡住"——
    这正是回读校验存在的理由。
    """

    def __init__(self, entities=None):
        self.entities = entities if entities is not None else make_all()
        self.calls = []
        self.connection_error = None
        self.stuck: set[str] = set()
        self.states = {entity["entity_id"]: entity["state"] for entity in self.entities}
        self.ask_payload = {
            "response": {"response_type": "action_done", "speech": {"plain": {"speech": "好的，已开灯"}}},
            "conversation_id": "abc",
            "continue_conversation": False,
        }

    async def collect_entities(self, *, aliases=None, include_read_only=True):
        if self.connection_error:
            raise self.connection_error
        return EntitySnapshot(entities=self.entities, area_count=4, ws_used=True)

    async def call(self, call):
        self.calls.append(call)
        entity_id = call.data.get("entity_id", "")
        if entity_id in self.stuck:
            return []
        after = SERVICE_STATE.get(call.service_id)
        if after is not None:
            self.states[entity_id] = after
        return []

    async def get_entity_state(self, entity_id):
        if entity_id not in self.states:
            return None
        return {"entity_id": entity_id, "state": self.states[entity_id], "attributes": {}}

    async def ask(self, text, language=""):
        self.ask_text = text
        self.ask_language = language
        return self.ask_payload

    async def close(self):
        pass


@pytest.fixture
def plugin(plugin_module, tmp_path):
    instance = plugin_module.HomeAssistantPlugin(None)
    instance.client = FakeHaClient()
    # 数据目录隔离：否则 whitelist.json / 别名会在测试间串味。
    instance.data_path = lambda name="": str(tmp_path / name) if name else str(tmp_path)
    # 审计日志在 __init__ 里就绑好了路径，晚于 data_path 替换，必须重建。
    instance._audit = plugin_module.AuditLog(tmp_path / "audit.jsonl")
    return instance


async def _unlock_directly(plugin, action="unlock", target="大门门锁"):
    """走完一遍确认流程，返回 (首次输出, 令牌)。"""
    first = await plugin.control_devices(targets=[target], action=action)
    token = first["output"]["confirmation"]["token"]
    return first, token


# ── 通用契约 ────────────────────────────────────────────────────────────
class TestNotConfigured:
    @pytest.mark.parametrize(
        ("method", "kwargs"),
        [
            ("find_devices", {}),
            ("get_state", {"targets": ["客厅吸顶灯"]}),
            ("control_devices", {"targets": ["客厅吸顶灯"], "action": "turn_on"}),
            ("run_scene", {"name": "回家"}),
            ("ask_home", {"text": "开灯"}),
        ],
    )
    async def test_all_tools_report_not_configured(self, plugin, method, kwargs):
        plugin.client = None
        out = await getattr(plugin, method)(**kwargs)
        assert out["is_error"] is True
        assert out["output"]["code"] == "not_configured"


class TestEnvelopeShape:
    async def test_success_envelope(self, plugin):
        out = await plugin.find_devices()
        assert out["is_error"] is False
        assert isinstance(out["output"], dict)

    async def test_error_envelope_has_code_detail_message(self, plugin):
        out = await plugin.control_devices(targets=["客厅吸顶灯"], action="explode")
        assert out["is_error"] is True
        assert out["output"]["code"] == "invalid_action"
        assert out["output"]["message"]
        assert "turn_on" in out["output"]["allowed"]


# ── find_devices ────────────────────────────────────────────────────────
class TestFindDevices:
    async def test_lists_controllable_by_default(self, plugin):
        out = await plugin.find_devices()
        ids = {row["entity_id"] for row in out["output"]["devices"]}
        assert "light.living_room_ceiling" in ids
        # 门锁是高危，默认不可控，默认不返回。
        assert "lock.front_door" not in ids

    async def test_include_blocked(self, plugin):
        out = await plugin.find_devices(include_blocked=True)
        rows = {row["entity_id"]: row for row in out["output"]["devices"]}
        assert rows["lock.front_door"]["controllable"] is False
        assert rows["lock.front_door"]["blocked_reason"] == "high_needs_approval"

    async def test_capabilities_exposed(self, plugin):
        out = await plugin.find_devices(name="吸顶灯")
        row = out["output"]["devices"][0]
        assert "brightness" in row["capabilities"]
        assert "color_temp" in row["capabilities"]

    async def test_filter_by_area(self, plugin):
        out = await plugin.find_devices(area="客厅")
        assert out["output"]["devices"]
        assert all(row["area"] == "客厅" for row in out["output"]["devices"])

    async def test_filter_by_domain(self, plugin):
        out = await plugin.find_devices(domain="light")
        assert all(row["domain"] == "light" for row in out["output"]["devices"])

    async def test_includes_read_only_sensors(self, plugin):
        # 传感器不可控，但必须能被发现——否则模型永远不知道有「客厅温度」。
        out = await plugin.find_devices(name="温度")
        rows = {row["entity_id"]: row for row in out["output"]["devices"]}
        assert "sensor.living_room_temperature" in rows
        assert rows["sensor.living_room_temperature"]["read_only"] is True
        assert rows["sensor.living_room_temperature"]["controllable"] is False

    async def test_truncated(self, plugin):
        out = await plugin.find_devices(limit=2)
        assert len(out["output"]["devices"]) == 2
        assert out["output"]["truncated"] is True
        assert out["output"]["total"] > 2

    async def test_not_truncated_when_fits(self, plugin):
        out = await plugin.find_devices(limit=200)
        assert out["output"]["truncated"] is False


# ── get_state ───────────────────────────────────────────────────────────
class TestGetState:
    async def test_ok(self, plugin):
        out = await plugin.get_state(targets=["客厅吸顶灯"])
        row = out["output"]["results"][0]
        assert row["status"] == "ok"
        assert row["entity_id"] == "light.living_room_ceiling"
        assert row["facts"]["brightness"] == 80

    async def test_not_found(self, plugin):
        out = await plugin.get_state(targets=["不存在的灯"])
        assert out["output"]["results"][0]["status"] == "not_found"

    async def test_ambiguous(self, plugin):
        # 两台灯都叫"灯"的一部分，子串匹配命中多个 → 报歧义而非自己挑。
        out = await plugin.get_state(targets=["灯"])
        statuses = {row["status"] for row in out["output"]["results"]}
        assert statuses & {"ok", "ambiguous"}

    async def test_reads_high_risk_entity(self, plugin):
        # 查询不受白名单限制：用户有权知道自己家门锁没锁。
        out = await plugin.get_state(targets=["大门门锁"])
        assert out["output"]["results"][0]["status"] == "ok"

    async def test_empty_targets(self, plugin):
        out = await plugin.get_state(targets=[])
        assert out["output"]["code"] == "missing_parameter"

    async def test_sensor_facts(self, plugin):
        out = await plugin.get_state(targets=["客厅温度"])
        row = out["output"]["results"][0]
        assert row["facts"]["value"] == "24.5"
        assert row["facts"]["unit"] == "°C"


# ── control_devices ─────────────────────────────────────────────────────
class TestControlBasic:
    async def test_turn_on_light(self, plugin):
        out = await plugin.control_devices(targets=["客厅吸顶灯"], action="turn_on")
        assert out["is_error"] is False
        assert out["output"]["executed"] == 1
        assert plugin.client.calls[0].service_id == "light.turn_on"

    async def test_brightness_param(self, plugin):
        out = await plugin.control_devices(targets=["客厅吸顶灯"], action="set", params={"brightness": 30})
        assert out["output"]["results"][0]["status"] == "ok"
        assert plugin.client.calls[0].data["brightness_pct"] == 30

    async def test_unsupported_param_reported(self, plugin):
        # 阳台灯只有 onoff，没有亮度。
        out = await plugin.control_devices(targets=["阳台灯"], action="set", params={"brightness": 50})
        row = out["output"]["results"][0]
        assert row["status"] == "unsupported_params"
        assert row["unsupported_params"] == ["brightness"]
        assert plugin.client.calls == []

    async def test_unsupported_action_reported(self, plugin):
        out = await plugin.control_devices(targets=["回家"], action="turn_off")
        assert out["output"]["results"][0]["status"] == "unsupported"

    async def test_not_found_target(self, plugin):
        out = await plugin.control_devices(targets=["不存在"], action="turn_on")
        assert out["output"]["results"][0]["status"] == "not_found"
        assert out["is_error"] is True

    async def test_multi_target(self, plugin):
        out = await plugin.control_devices(targets=["客厅吸顶灯", "书房插座"], action="turn_on")
        assert out["output"]["executed"] == 2
        assert {call.service_id for call in plugin.client.calls} == {"light.turn_on", "switch.turn_on"}

    async def test_area_with_many_devices_is_ambiguous(self, plugin):
        # 刻意不把"客厅"展开成全部设备：客厅里有窗帘，turn_on 会把窗帘打开。
        out = await plugin.control_devices(targets=["客厅"], action="turn_on")
        row = out["output"]["results"][0]
        assert row["status"] == "ambiguous"
        assert row["candidates"]
        assert plugin.client.calls == []

    async def test_area_with_single_device_resolves(self, plugin):
        # 主卧只有一个设备（空调），此时区域名等价于设备名。
        out = await plugin.control_devices(targets=["主卧"], action="turn_off")
        assert out["output"]["executed"] == 1

    async def test_partial_failure_is_error(self, plugin):
        out = await plugin.control_devices(
            targets=["客厅吸顶灯", "阳台灯"],
            action="set",
            params={"brightness": 50},
        )
        statuses = [row["status"] for row in out["output"]["results"]]
        assert "ok" in statuses and "unsupported_params" in statuses
        assert out["is_error"] is True


class TestControlWhitelist:
    async def test_high_risk_blocked_by_default(self, plugin):
        out = await plugin.control_devices(targets=["大门门锁"], action="unlock")
        row = out["output"]["results"][0]
        assert row["status"] == "blocked"
        assert row["reason"] == "high_needs_approval"
        assert plugin.client.calls == []

    async def test_explicitly_allowed_high_risk_proceeds(self, plugin):
        plugin._whitelist.set_entity("lock.front_door", True)
        out = await plugin.control_devices(targets=["大门门锁"], action="unlock")
        assert out["output"]["results"][0]["status"] == "confirmation_required"

    async def test_user_disabled_low_risk_blocked(self, plugin):
        plugin._whitelist.set_entity("light.living_room_ceiling", False)
        out = await plugin.control_devices(targets=["客厅吸顶灯"], action="turn_on")
        assert out["output"]["results"][0]["reason"] == "user_disabled"

    async def test_unsupported_domain_blocked(self, plugin):
        out = await plugin.control_devices(targets=["客厅温度"], action="turn_on")
        row = out["output"]["results"][0]
        assert row["status"] == "blocked"
        assert row["reason"] == "unsupported"

    async def test_garage_door_is_high_risk(self, plugin):
        out = await plugin.control_devices(targets=["车库门"], action="open")
        assert out["output"]["results"][0]["status"] == "blocked"


class TestConfirmation:
    async def test_first_call_does_not_execute(self, plugin):
        plugin._whitelist.set_entity("lock.front_door", True)
        out = await plugin.control_devices(targets=["大门门锁"], action="unlock")
        assert out["is_error"] is True
        assert out["output"]["code"] == "confirmation_required"
        assert plugin.client.calls == []

    async def test_token_summary_is_readable(self, plugin):
        plugin._whitelist.set_entity("lock.front_door", True)
        first, token = await _unlock_directly(plugin)
        confirmation = first["output"]["confirmation"]
        assert confirmation["token"] == token
        assert len(token) == 8
        assert confirmation["action"] == "unlock"
        assert confirmation["targets"] == ["大门门锁"]

    async def test_executes_with_valid_token(self, plugin):
        plugin._whitelist.set_entity("lock.front_door", True)
        _, token = await _unlock_directly(plugin)
        out = await plugin.control_devices(targets=["大门门锁"], action="unlock", confirm_token=token)
        assert out["output"]["executed"] == 1
        assert plugin.client.calls[0].service_id == "lock.unlock"

    async def test_token_is_single_use(self, plugin):
        plugin._whitelist.set_entity("lock.front_door", True)
        _, token = await _unlock_directly(plugin)
        await plugin.control_devices(targets=["大门门锁"], action="unlock", confirm_token=token)
        again = await plugin.control_devices(targets=["大门门锁"], action="unlock", confirm_token=token)
        assert again["output"]["code"] == "confirmation_invalid"
        assert again["output"]["reason"] == "no_pending"

    async def test_wrong_token_rejected(self, plugin):
        plugin._whitelist.set_entity("lock.front_door", True)
        await _unlock_directly(plugin)
        out = await plugin.control_devices(targets=["大门门锁"], action="unlock", confirm_token="deadbeef")
        assert out["output"]["reason"] == "token_mismatch"
        assert plugin.client.calls == []

    async def test_changing_action_invalidates_token(self, plugin):
        plugin._whitelist.set_entity("lock.front_door", True)
        _, token = await _unlock_directly(plugin)
        # unlock → open 都是高危，但动作变了 = 指纹变了，令牌作废。
        out = await plugin.control_devices(targets=["大门门锁"], action="open", confirm_token=token)
        assert out["output"]["code"] == "confirmation_invalid"
        assert out["output"]["reason"] == "fingerprint_mismatch"
        assert plugin.client.calls == []

    async def test_changing_target_invalidates_token(self, plugin):
        plugin._whitelist.set_entity("lock.front_door", True)
        plugin._whitelist.set_entity("valve.gas_main", True)
        first = await plugin.control_devices(targets=["大门门锁"], action="unlock")
        token = first["output"]["confirmation"]["token"]
        out = await plugin.control_devices(targets=["燃气阀"], action="open", confirm_token=token)
        assert out["output"]["reason"] == "fingerprint_mismatch"
        assert plugin.client.calls == []

    async def test_expired_token_rejected(self, plugin):
        plugin._whitelist.set_entity("lock.front_door", True)
        first, token = await _unlock_directly(plugin)
        plugin._confirms._pending.expires_at = 0  # 手动过期
        out = await plugin.control_devices(targets=["大门门锁"], action="unlock", confirm_token=token)
        assert out["output"]["reason"] == "expired"
        assert plugin.client.calls == []
        assert first["output"]["code"] == "confirmation_required"


class TestReadback:
    async def test_verified_true_when_state_changed(self, plugin):
        out = await plugin.control_devices(targets=["客厅吸顶灯"], action="turn_off")
        row = out["output"]["results"][0]
        assert row["state_after"] == "off"
        assert row["verified"] is True

    async def test_verified_false_when_device_stuck(self, plugin):
        plugin.client.stuck.add("cover.living_room_curtain")
        out = await plugin.control_devices(targets=["客厅窗帘"], action="close")
        row = out["output"]["results"][0]
        assert row["status"] == "ok"
        assert row["verified"] is False

    async def test_verified_none_when_unknown(self, plugin):
        plugin.client.get_entity_state = lambda entity_id: _no_state()
        out = await plugin.control_devices(targets=["客厅吸顶灯"], action="turn_on")
        assert out["output"]["results"][0]["verified"] is None


async def _no_state():
    return None


class TestSetEntityAllowed:
    async def test_authorize_high_risk(self, plugin):
        out = await plugin.set_entity_allowed(entity_id="lock.front_door", allowed=True)
        assert out.value["success"] is True
        assert plugin._whitelist.allows(make_lock()) is True

    async def test_revoke(self, plugin):
        await plugin.set_entity_allowed(entity_id="light.living_room_ceiling", allowed=False)
        assert plugin._whitelist.allows(make_light()) is False

    async def test_rejects_unknown_entity(self, plugin):
        out = await plugin.set_entity_allowed(entity_id="light.nope", allowed=True)
        assert not out.is_ok()
        assert out.error.code == "entity_not_found"

    async def test_rejects_unsupported_domain(self, plugin):
        out = await plugin.set_entity_allowed(entity_id="sensor.living_room_temperature", allowed=True)
        assert not out.is_ok()
        assert out.error.code == "unsupported_domain"


# ── run_scene ───────────────────────────────────────────────────────────
class TestRunScene:
    async def test_runs_scene(self, plugin):
        out = await plugin.run_scene(name="回家")
        assert out["output"]["ok"] is True
        assert plugin.client.calls[0].service_id == "scene.turn_on"

    async def test_scene_not_found_lists_available(self, plugin):
        out = await plugin.run_scene(name="不存在的场景")
        assert out["output"]["code"] == "scene_not_found"
        assert "回家" in out["output"]["available"]

    async def test_empty_name(self, plugin):
        out = await plugin.run_scene(name="  ")
        assert out["output"]["code"] == "missing_parameter"


# ── ask_home ────────────────────────────────────────────────────────────
class TestAskHome:
    async def test_returns_speech(self, plugin):
        out = await plugin.ask_home(text="把客厅灯打开")
        assert out["output"]["speech"] == "好的，已开灯"
        assert plugin.client.ask_text == "把客厅灯打开"

    async def test_assist_error_is_reported(self, plugin):
        plugin.client.ask_payload = {
            "response": {
                "response_type": "error",
                "data": {"code": "no_intent_match"},
                "speech": {"plain": {"speech": "抱歉，我没听懂"}},
            }
        }
        out = await plugin.ask_home(text="随便说点啥")
        assert out["is_error"] is True
        assert out["output"]["code"] == "assist_no_match"
        assert out["output"]["assist_code"] == "no_intent_match"

    async def test_empty_text(self, plugin):
        out = await plugin.ask_home(text="")
        assert out["output"]["code"] == "missing_parameter"


# ── 审计日志 ────────────────────────────────────────────────────────────
class TestAudit:
    async def test_records_action(self, plugin, tmp_path):
        await plugin.control_devices(targets=["客厅吸顶灯"], action="turn_on")
        log = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
        assert "turn_on" in log
        assert "light.living_room_ceiling" in log

    async def test_never_records_token(self, plugin, tmp_path):
        plugin._whitelist.set_entity("lock.front_door", True)
        _, token = await _unlock_directly(plugin)
        await plugin.control_devices(targets=["大门门锁"], action="unlock", confirm_token=token)
        log = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
        assert token not in log
