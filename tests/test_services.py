"""语义动作 → HA 服务调用映射与危险方向判定。

这一层的价值全在"细节对不对"上：服务名、字段名、量纲换算、危险方向。
所以测试按 domain 分组，每条断言都对着 HA 的官方服务定义。
"""

import pytest
from _fixtures import (
    make_alarm,
    make_climate,
    make_cover,
    make_fan,
    make_garage,
    make_light,
    make_lock,
    make_media,
    make_plain_light,
    make_scene,
    make_switch,
    make_valve,
    make_water_heater,
)

# isort: split
from ha_api.services import ACTIONS, build_call, is_dangerous, scene_call


def call_for(entity, action, params=None):
    """便捷包装：返回 ServiceCall，断言它一定编出来了。"""
    call, unsupported = build_call(entity, action, params)
    assert call is not None, f"{entity.get('domain')} 不支持 {action}"
    assert unsupported == [], f"意外的不支持参数：{unsupported}"
    return call


class TestPowerActions:
    def test_light_turn_on(self):
        call = call_for(make_light(), "turn_on")
        assert call.service_id == "light.turn_on"
        assert call.data == {"entity_id": "light.living_room_ceiling"}
        assert call.dangerous is False

    def test_switch_turn_off(self):
        assert call_for(make_switch(), "turn_off").service_id == "switch.turn_off"

    def test_toggle(self):
        assert call_for(make_light(), "toggle").service_id == "light.toggle"

    def test_climate_turn_on(self):
        assert call_for(make_climate(), "turn_on").service_id == "climate.turn_on"

    def test_fan_turn_on(self):
        assert call_for(make_fan(), "turn_on").service_id == "fan.turn_on"

    def test_water_heater_turn_on_is_dangerous(self):
        # 给热水器通电 = 可能加热到烫伤温度，判高危。
        call = call_for(make_water_heater(), "turn_on")
        assert call.service_id == "water_heater.turn_on"
        assert call.dangerous is True

    def test_water_heater_turn_off_is_safe(self):
        assert call_for(make_water_heater(), "turn_off").dangerous is False


class TestCoverAndValve:
    def test_cover_open(self):
        assert call_for(make_cover(), "open").service_id == "cover.open_cover"

    def test_cover_close(self):
        assert call_for(make_cover(), "close").service_id == "cover.close_cover"

    def test_curtain_open_is_not_dangerous(self):
        assert call_for(make_cover(), "open").dangerous is False

    def test_garage_open_is_dangerous(self):
        assert call_for(make_garage(), "open").dangerous is True

    def test_garage_close_is_safe(self):
        assert call_for(make_garage(), "close").dangerous is False

    def test_valve_open_is_dangerous(self):
        call = call_for(make_valve(), "open")
        assert call.service_id == "valve.open_valve"
        assert call.dangerous is True

    def test_valve_close_is_safe(self):
        assert call_for(make_valve(), "close").dangerous is False

    def test_cover_position(self):
        call = call_for(make_cover(), "set", {"position": 30})
        assert call.service_id == "cover.set_cover_position"
        assert call.data["position"] == 30

    def test_valve_position(self):
        call = call_for(make_valve(), "set", {"position": 100})
        assert call.service_id == "valve.set_valve_position"


class TestLockSemantics:
    def test_lock(self):
        call = call_for(make_lock(), "lock")
        assert call.service_id == "lock.lock"
        assert call.dangerous is False

    def test_unlock_is_dangerous(self):
        call = call_for(make_lock(), "unlock")
        assert call.service_id == "lock.unlock"
        assert call.dangerous is True

    def test_open_on_lock_keeps_open_service(self):
        # HA 的 lock 有 open 服务（开门闩，不是解锁），两个方向都是高危。
        call = call_for(make_lock(), "open")
        assert call.service_id == "lock.open"
        assert call.dangerous is True

    def test_close_on_lock_maps_to_lock(self):
        call = call_for(make_lock(), "close")
        assert call.service_id == "lock.lock"
        assert call.dangerous is False

    def test_lock_does_not_accept_turn_on(self):
        call, _ = build_call(make_lock(), "turn_on")
        assert call is None


class TestAlarm:
    @pytest.mark.parametrize(
        ("action", "service"),
        [
            ("arm_home", "alarm_arm_home"),
            ("arm_away", "alarm_arm_away"),
            ("arm_night", "alarm_arm_night"),
            ("disarm", "alarm_disarm"),
        ],
    )
    def test_arm_and_disarm(self, action, service):
        assert call_for(make_alarm(), action).service_id == f"alarm_control_panel.{service}"

    def test_only_disarm_is_dangerous(self):
        assert call_for(make_alarm(), "disarm").dangerous is True
        assert call_for(make_alarm(), "arm_away").dangerous is False


class TestLightParams:
    def test_brightness(self):
        call = call_for(make_light(), "set", {"brightness": 60})
        assert call.service_id == "light.turn_on"
        assert call.data["brightness_pct"] == 60

    def test_brightness_zero_becomes_turn_off(self):
        # HA 的 brightness_pct 合法区间是 1-100，0 只能翻成 turn_off。
        call = call_for(make_light(), "set", {"brightness": 0})
        assert call.service_id == "light.turn_off"
        assert "brightness_pct" not in call.data

    def test_brightness_clamped(self):
        assert call_for(make_light(), "set", {"brightness": 150}).data["brightness_pct"] == 100

    def test_color_temp(self):
        call = call_for(make_light(), "set", {"color_temp": 3000})
        assert call.data["color_temp_kelvin"] == 3000

    def test_color_hex(self):
        call = call_for(make_light(), "set", {"color": "#FF8800"})
        assert call.data["rgb_color"] == [255, 136, 0]

    def test_color_rgb_list(self):
        assert call_for(make_light(), "set", {"color": [1, 2, 3]}).data["rgb_color"] == [1, 2, 3]

    def test_plain_light_rejects_brightness(self):
        _, unsupported = build_call(make_plain_light(), "set", {"brightness": 50})
        assert unsupported == ["brightness"]

    def test_switch_rejects_brightness(self):
        _, unsupported = build_call(make_switch(), "set", {"brightness": 50})
        assert unsupported == ["brightness"]


class TestClimateParams:
    def test_temperature(self):
        call = call_for(make_climate(), "set", {"temperature": 24.5})
        assert call.service_id == "climate.set_temperature"
        assert call.data["temperature"] == 24.5

    def test_hvac_mode(self):
        call = call_for(make_climate(), "set", {"hvac_mode": "heat"})
        assert call.service_id == "climate.set_hvac_mode"
        assert call.data["hvac_mode"] == "heat"

    def test_fan_speed_maps_to_fan_mode(self):
        call = call_for(make_climate(), "set", {"fan_speed": "high"})
        assert call.service_id == "climate.set_fan_mode"
        assert call.data["fan_mode"] == "high"

    def test_preset(self):
        call = call_for(make_climate(), "set", {"preset": "eco"})
        assert call.service_id == "climate.set_preset_mode"
        assert call.data["preset_mode"] == "eco"

    def test_temperature_rejected_on_light(self):
        _, unsupported = build_call(make_light(), "set", {"temperature": 24})
        assert unsupported == ["temperature"]


class TestFanParams:
    def test_percentage(self):
        call = call_for(make_fan(), "set", {"percentage": 40})
        assert call.service_id == "fan.set_percentage"
        assert call.data["percentage"] == 40

    def test_preset(self):
        call = call_for(make_fan(), "set", {"preset": "sleep"})
        assert call.service_id == "fan.set_preset_mode"
        assert call.data["preset_mode"] == "sleep"


class TestMediaParams:
    def test_volume_converts_to_0_1(self):
        call = call_for(make_media(), "set", {"volume": 35})
        assert call.service_id == "media_player.volume_set"
        assert call.data["volume_level"] == 0.35

    def test_source(self):
        call = call_for(make_media(), "set", {"source": "HDMI1"})
        assert call.service_id == "media_player.select_source"
        assert call.data["source"] == "HDMI1"

    def test_volume_rejected_on_fan(self):
        _, unsupported = build_call(make_fan(), "set", {"volume": 30})
        assert unsupported == ["volume"]

    @pytest.mark.parametrize(
        ("action", "service"),
        [
            ("play", "media_play"),
            ("pause", "media_pause"),
            ("next", "media_next_track"),
            ("previous", "media_previous_track"),
            ("stop", "media_stop"),
        ],
    )
    def test_transport(self, action, service):
        assert call_for(make_media(), action).service_id == f"media_player.{service}"

    def test_mute(self):
        call = call_for(make_media(), "mute")
        assert call.service_id == "media_player.volume_mute"
        assert call.data["is_volume_muted"] is True

    def test_unmute(self):
        assert call_for(make_media(), "unmute").data["is_volume_muted"] is False


class TestWaterHeaterParams:
    def test_set_temperature_is_dangerous(self):
        call = call_for(make_water_heater(), "set", {"temperature": 55})
        assert call.service_id == "water_heater.set_temperature"
        assert call.dangerous is True

    def test_operation_mode(self):
        call = call_for(make_water_heater(), "set", {"operation": "eco"})
        assert call.service_id == "water_heater.set_operation_mode"
        assert call.data["operation_mode"] == "eco"


class TestUnsupported:
    def test_scene_has_no_turn_off(self):
        call, _ = build_call(make_scene("回家"), "turn_off")
        assert call is None

    def test_unknown_domain(self):
        call, _ = build_call({"entity_id": "vacuum.x", "domain": "vacuum"}, "turn_on")
        assert call is None

    def test_invalid_action(self):
        call, _ = build_call(make_light(), "explode")
        assert call is None

    def test_set_without_usable_params(self):
        call, _ = build_call(make_cover(), "set", {})
        assert call is None


class TestDangerousJudgement:
    def test_dangerous_services_table(self):
        assert is_dangerous("lock", "unlock") is True
        assert is_dangerous("lock", "lock") is False
        assert is_dangerous("alarm_control_panel", "alarm_disarm") is True
        assert is_dangerous("valve", "open_valve") is True
        assert is_dangerous("water_heater", "turn_on") is True

    def test_cover_depends_on_device_class(self):
        garage = {"caps": {"device_class": "garage"}}
        curtain = {"caps": {"device_class": "curtain"}}
        assert is_dangerous("cover", "open_cover", garage) is True
        assert is_dangerous("cover", "open_cover", curtain) is False

    def test_cover_close_never_dangerous(self):
        assert is_dangerous("cover", "close_cover", {"caps": {"device_class": "garage"}}) is False


class TestScene:
    def test_scene_call(self):
        call = scene_call(make_scene("回家", entity_id="scene.home_arrival"))
        assert call.service_id == "scene.turn_on"
        assert call.data["entity_id"] == "scene.home_arrival"
        assert call.dangerous is False

    def test_script_call_keeps_domain(self):
        script = {"entity_id": "script.good_night", "domain": "script"}
        assert scene_call(script).service_id == "script.turn_on"


class TestActionEnums:
    def test_actions_are_lowercase_snake(self):
        for action in ACTIONS:
            assert action == action.lower()
            assert " " not in action

    def test_brief_excludes_entity_id(self):
        call = call_for(make_light(), "set", {"brightness": 50})
        assert "entity_id" not in call.brief()
        assert "brightness_pct" in call.brief()
