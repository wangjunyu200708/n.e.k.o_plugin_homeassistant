"""@llm_tool 契约：信封结构、错误码、端到端控制。"""

import pytest
from _fixtures import make_all
from ha_api.client import EntitySnapshot


class FakeHaClient:
    """假 HA：collect_entities 返回固定快照，call 记录服务调用。"""

    def __init__(self, entities=None):
        self.entities = entities if entities is not None else make_all()
        self.calls = []
        self.connection_error = None

    async def collect_entities(self, *, aliases=None, include_read_only=True):
        if self.connection_error:
            raise self.connection_error
        return EntitySnapshot(entities=self.entities, area_count=4, ws_used=True)

    async def call(self, call):
        self.calls.append(call)
        return []

    async def get_entity_state(self, entity_id):
        return None

    async def close(self):
        pass


@pytest.fixture
def plugin(plugin_module):
    instance = plugin_module.HomeAssistantPlugin(None)
    instance.client = FakeHaClient()
    return instance


class TestMissingCommand:
    async def test_none(self, plugin):
        out = await plugin.smart_control(command=None)
        assert out["is_error"] is True
        assert out["output"]["code"] == "missing_command"

    async def test_empty(self, plugin):
        out = await plugin.smart_control(command="   ")
        assert out["output"]["code"] == "missing_command"

    async def test_wrong_type(self, plugin):
        out = await plugin.smart_control(command=123)
        assert out["output"]["code"] == "missing_command"

    async def test_detail_explains_expected(self, plugin):
        out = await plugin.smart_control(command=None)
        assert out["output"]["detail"]["param"] == "command"


class TestNotConfigured:
    async def test_returns_not_configured(self, plugin):
        plugin.client = None
        out = await plugin.smart_control(command="打开客厅灯")
        assert out["is_error"] is True
        assert out["output"]["code"] == "not_configured"
        assert "令牌" in out["output"]["message"]


class TestEnvelope:
    def test_ok(self, plugin_module):
        env = plugin_module._tool_envelope(plugin_module.Ok({"success": True}))
        assert env == {"output": {"success": True}, "is_error": False}

    def test_err_carries_code_and_detail(self, plugin_module):
        env = plugin_module._tool_envelope(plugin_module._err("demo_code", "人话", candidates=[{"name": "灯"}]))
        assert env["is_error"] is True
        assert env["error"] == "demo_code"
        assert env["output"]["code"] == "demo_code"
        assert env["output"]["detail"]["candidates"][0]["name"] == "灯"

    def test_err_without_code_falls_back(self, plugin_module):
        env = plugin_module._tool_envelope(plugin_module.Err(plugin_module.SdkError("炸了")))
        assert env["output"]["code"] == "control_failed"

    def test_plain_value(self, plugin_module):
        assert plugin_module._tool_envelope({"a": 1}) == {"output": {"a": 1}, "is_error": False}


class TestDescribeResult:
    def test_ok(self, plugin_module):
        item = plugin_module._describe_result("打开灯", plugin_module.Ok({"message": "已完成"}))
        assert item == {"command": "打开灯", "success": True, "message": "已完成"}

    def test_err(self, plugin_module):
        item = plugin_module._describe_result("打开灯", plugin_module._err("entity_not_found", "没找到"))
        assert item["success"] is False
        assert item["code"] == "entity_not_found"


class TestEndToEnd:
    async def test_open_light(self, plugin):
        out = await plugin.smart_control(command="打开客厅吸顶灯")
        assert out["is_error"] is False
        assert out["output"]["success"] is True
        assert plugin.client.calls[0].service_id == "light.turn_on"

    async def test_brightness(self, plugin):
        out = await plugin.smart_control(command="客厅吸顶灯亮度50%")
        assert out["is_error"] is False
        assert plugin.client.calls[0].data["brightness_pct"] == 50

    async def test_climate_temperature(self, plugin):
        out = await plugin.smart_control(command="主卧空调26度")
        assert out["is_error"] is False
        assert plugin.client.calls[0].service_id == "climate.set_temperature"
        assert plugin.client.calls[0].data["temperature"] == 26

    async def test_climate_mode(self, plugin):
        out = await plugin.smart_control(command="主卧空调制冷")
        assert out["is_error"] is False
        assert plugin.client.calls[0].data["hvac_mode"] == "cool"

    async def test_cover_half(self, plugin):
        out = await plugin.smart_control(command="客厅窗帘开到一半")
        assert out["is_error"] is False
        assert plugin.client.calls[0].service_id == "cover.set_cover_position"
        assert plugin.client.calls[0].data["position"] == 50

    async def test_volume(self, plugin):
        out = await plugin.smart_control(command="客厅音箱音量30%")
        assert out["is_error"] is False
        assert plugin.client.calls[0].data["volume_level"] == 0.3

    async def test_adjust_down_uses_current_value(self, plugin):
        out = await plugin.smart_control(command="客厅吸顶灯调暗一点")
        assert out["is_error"] is False
        # 当前 80% → 下降一个默认步长 10% → 70%
        assert plugin.client.calls[0].data["brightness_pct"] == 70

    async def test_scene(self, plugin):
        out = await plugin.smart_control(command="执行回家场景")
        assert out["is_error"] is False
        assert plugin.client.calls[0].service_id == "scene.turn_on"

    async def test_scene_missing(self, plugin):
        out = await plugin.smart_control(command="执行不存在的场景")
        assert out["is_error"] is True
        assert out["output"]["code"] == "scene_not_found"

    async def test_query_sensor(self, plugin):
        out = await plugin.smart_control(command="客厅温度多少度")
        assert out["is_error"] is False
        assert "24.5" in out["output"]["message"]

    async def test_unknown_device(self, plugin):
        out = await plugin.smart_control(command="打开月球上的灯")
        assert out["is_error"] is True
        assert out["output"]["code"] == "entity_not_found"

    async def test_ambiguous_device(self, plugin):
        from _fixtures import make_entity

        plugin.client = FakeHaClient([make_entity("light.a", "顶灯"), make_entity("light.b", "顶灯")])
        out = await plugin.smart_control(command="打开顶灯")
        assert out["output"]["code"] == "entity_ambiguous"
        assert len(out["output"]["detail"]["candidates"]) == 2

    async def test_invalid_unit(self, plugin):
        out = await plugin.smart_control(command="客厅吸顶灯亮度调到三档")
        assert out["is_error"] is True
        assert out["output"]["code"] in ("invalid_unit", "invalid_value")

    async def test_attribute_not_supported(self, plugin):
        # 开关没有亮度
        out = await plugin.smart_control(command="书房插座亮度50%")
        assert out["is_error"] is True
        assert out["output"]["code"] in ("unknown_attribute", "attribute_not_supported")

    async def test_unsupported_domain(self, plugin):
        from _fixtures import make_entity

        plugin.client = FakeHaClient([make_entity("vacuum.robot", "扫地机", state="docked")])
        out = await plugin.smart_control(command="打开扫地机")
        assert out["is_error"] is True
        assert out["output"]["code"] == "unsupported_domain"

    async def test_multi_command_all_succeed(self, plugin):
        out = await plugin.smart_control(command="打开客厅吸顶灯和关掉书房插座")
        assert out["is_error"] is False
        assert out["output"]["total"] == 2
        assert len(plugin.client.calls) == 2

    async def test_multi_command_partial_failure(self, plugin):
        # 两段都能路由到实体（预检通过），但第二段执行时属性不支持 → 部分失败
        out = await plugin.smart_control(command="打开客厅吸顶灯和书房插座亮度50%")
        assert out["is_error"] is True
        assert out["output"]["code"] == "multi_control_partial_failure"
        assert out["output"]["detail"]["succeeded"] == 1
        assert out["output"]["detail"]["failed"] == 1

    async def test_multi_command_rolls_back_when_segment_unroutable(self, plugin):
        # 第二段找不到设备 → 整句回退单指令，不当成两条
        out = await plugin.smart_control(command="打开客厅吸顶灯和打开月球灯")
        assert out["output"].get("total") is None

    async def test_unlock_requires_confirmation(self, plugin):
        from _fixtures import make_lock

        plugin.client = FakeHaClient([make_lock()])
        out = await plugin.smart_control(command="解锁大门门锁")
        assert out["is_error"] is True
        assert out["output"]["code"] == "confirmation_required"
        assert plugin.client.calls == []

    async def test_lock_is_safe(self, plugin):
        from _fixtures import make_lock

        plugin.client = FakeHaClient([make_lock()])
        out = await plugin.smart_control(command="锁上大门门锁")
        assert out["is_error"] is False
        assert plugin.client.calls[0].service_id == "lock.lock"
