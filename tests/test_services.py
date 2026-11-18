"""语义动作 → HA 服务调用映射与危险判定。"""

from _fixtures import make_climate, make_cover, make_garage, make_light, make_lock, make_media, make_scene, make_switch
from ha_api.services import action_call, power_call, prop_call, scene_call


class TestPowerCall:
    def test_light(self):
        call = power_call(make_light(), True)
        assert call.service_id == "light.turn_on"
        assert call.data["entity_id"] == "light.living_room_ceiling"
        assert call.dangerous is False

    def test_switch_off(self):
        assert power_call(make_switch(), False).service_id == "switch.turn_off"

    def test_climate_uses_turn_on(self):
        assert power_call(make_climate(), True).service_id == "climate.turn_on"

    def test_cover_uses_open_close(self):
        assert power_call(make_cover(), True).service_id == "cover.open_cover"
        assert power_call(make_cover(), False).service_id == "cover.close_cover"

    def test_media(self):
        assert power_call(make_media(), False).service_id == "media_player.turn_off"

    def test_lock_open_means_unlock(self):
        # 门锁的"打开"是解锁（危险），"关闭"是上锁（安全）
        call = power_call(make_lock(), True)
        assert call.service_id == "lock.unlock"
        assert call.dangerous is True

    def test_lock_close_is_safe(self):
        call = power_call(make_lock(), False)
        assert call.service_id == "lock.lock"
        assert call.dangerous is False

    def test_scene_only_on(self):
        assert power_call(make_scene(), True).service_id == "scene.turn_on"
        assert power_call(make_scene(), False) is None

    def test_garage_open_is_dangerous(self):
        call = power_call(make_garage(), True)
        assert call.service_id == "cover.open_cover"
        assert call.dangerous is True

    def test_garage_close_is_safe(self):
        assert power_call(make_garage(), False).dangerous is False

    def test_unknown_domain(self):
        from _fixtures import make_entity

        assert power_call(make_entity("input_text.x", "文本", state=""), True) is None


class TestPropCall:
    def test_brightness_uses_pct(self):
        call = prop_call(make_light(), "brightness", 50)
        assert call.service_id == "light.turn_on"
        assert call.data["brightness_pct"] == 50

    def test_brightness_zero_turns_off(self):
        call = prop_call(make_light(), "brightness", 0)
        assert call.service_id == "light.turn_off"
        assert "brightness_pct" not in call.data

    def test_brightness_clamped(self):
        assert prop_call(make_light(), "brightness", 150).data["brightness_pct"] == 100

    def test_color_temp(self):
        call = prop_call(make_light(), "color_temp", 3000)
        assert call.data["color_temp_kelvin"] == 3000

    def test_color_int(self):
        call = prop_call(make_light(), "color", 0xFF0000)
        assert call.data["rgb_color"] == [255, 0, 0]

    def test_color_hex_string(self):
        assert prop_call(make_light(), "color", "#00FF00").data["rgb_color"] == [0, 255, 0]

    def test_color_list(self):
        assert prop_call(make_light(), "color", [0, 0, 255]).data["rgb_color"] == [0, 0, 255]

    def test_temperature(self):
        call = prop_call(make_climate(), "temperature", 26.5)
        assert call.service_id == "climate.set_temperature"
        assert call.data["temperature"] == 26.5

    def test_hvac_mode(self):
        call = prop_call(make_climate(), "hvac_modes", "cool")
        assert call.service_id == "climate.set_hvac_mode"
        assert call.data["hvac_mode"] == "cool"

    def test_fan_mode(self):
        call = prop_call(make_climate(), "fan_modes", "high")
        assert call.service_id == "climate.set_fan_mode"
        assert call.data["fan_mode"] == "high"

    def test_cover_position(self):
        call = prop_call(make_cover(), "position", 50)
        assert call.service_id == "cover.set_cover_position"
        assert call.data["position"] == 50

    def test_volume_converted_to_ratio(self):
        call = prop_call(make_media(), "volume", 30)
        assert call.service_id == "media_player.volume_set"
        assert call.data["volume_level"] == 0.3

    def test_volume_zero(self):
        assert prop_call(make_media(), "volume", 0).data["volume_level"] == 0.0

    def test_source(self):
        call = prop_call(make_media(), "source_list", "HDMI1")
        assert call.service_id == "media_player.select_source"
        assert call.data["source"] == "HDMI1"

    def test_effect(self):
        entity = make_light(attributes={"effect_list": ["Rainbow"]})
        call = prop_call(entity, "effect_list", "Rainbow")
        assert call.data["effect"] == "Rainbow"

    def test_unsupported_combo(self):
        assert prop_call(make_switch(), "brightness", 50) is None

    def test_non_numeric_brightness_rejected(self):
        assert prop_call(make_light(), "brightness", "很亮") is None


class TestActionCall:
    def test_media_play(self):
        assert action_call(make_media(), "播放").service_id == "media_player.media_play"

    def test_media_next(self):
        assert action_call(make_media(), "下一首").service_id == "media_player.media_next_track"

    def test_media_mute_sets_flag(self):
        call = action_call(make_media(), "静音")
        assert call.service_id == "media_player.volume_mute"
        assert call.data["is_volume_muted"] is True

    def test_cover_stop(self):
        assert action_call(make_cover(), "停止").service_id == "cover.stop_cover"

    def test_unknown_verb(self):
        assert action_call(make_media(), "快退") is None

    def test_scene(self):
        assert action_call(make_scene(), "任何").service_id == "scene.turn_on"


class TestSceneCall:
    def test_scene(self):
        call = scene_call(make_scene())
        assert call.service_id == "scene.turn_on"
        assert call.dangerous is False

    def test_script(self):
        from _fixtures import make_entity

        script = make_entity("script.morning", "早间", state="off")
        assert scene_call(script).service_id == "script.turn_on"
