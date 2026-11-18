"""能力归一化：domain + attributes → caps。"""

from _fixtures import (
    make_climate,
    make_cover,
    make_entity,
    make_garage,
    make_light,
    make_lock,
    make_media,
    make_plain_light,
    make_sensor,
)
from ha_api.caps import UNSUPPORTED_DOMAINS, build_caps, caps_unit, is_read_only, is_supported


class TestLight:
    def test_full_light_has_power_brightness_color_temp_color(self):
        caps = make_light()["caps"]
        assert caps["power"] is True
        assert caps["brightness"] == {"min": 1, "max": 100}
        assert caps["color_temp"] == {"min": 2000, "max": 6500}
        assert caps["color"] is True

    def test_onoff_only_light_has_no_brightness(self):
        caps = make_plain_light()["caps"]
        assert caps["power"] is True
        assert "brightness" not in caps
        assert "color" not in caps

    def test_color_temp_absent_without_kelvin_attributes(self):
        entity = make_entity(
            "light.x", "测试灯", attributes={"supported_color_modes": ["brightness"], "brightness": 100}
        )
        assert "color_temp" not in entity["caps"]


class TestClimate:
    def test_climate_caps(self):
        caps = make_climate()["caps"]
        assert caps["power"] is True
        assert caps["hvac_modes"] == ["off", "cool", "heat", "auto", "dry", "fan_only"]
        assert caps["temperature"] == {"min": 16.0, "max": 30.0, "step": 0.5}
        assert caps["fan_modes"] == ["low", "medium", "high", "auto"]
        assert caps["preset_modes"] == ["none", "eco", "boost"]

    def test_missing_min_max_temp_yields_no_temperature_cap(self):
        entity = make_entity("climate.x", "空调", attributes={"hvac_modes": ["off", "cool"]})
        assert "temperature" not in entity["caps"]


class TestCover:
    def test_curtain_is_not_dangerous(self):
        caps = make_cover()["caps"]
        assert caps["position"] == {"min": 0, "max": 100}
        assert caps["device_class"] == "curtain"
        assert "dangerous" not in caps

    def test_garage_open_is_dangerous(self):
        caps = make_garage()["caps"]
        assert caps["dangerous"] == ["open_cover"]

    def test_gate_is_dangerous(self):
        entity = make_cover("大门", device_class="gate")
        assert entity["caps"]["dangerous"] == ["open_cover"]


class TestOthers:
    def test_media_caps(self):
        caps = make_media()["caps"]
        assert caps["volume"] == {"min": 0, "max": 100}
        assert "HDMI1" in caps["source_list"]
        assert "play" in caps["media_actions"]
        assert "next" in caps["media_actions"]
        assert "previous" in caps["media_actions"]
        assert caps["mute"] is True

    def test_lock_is_dangerous(self):
        assert make_lock()["caps"]["dangerous"] == ["unlock"]

    def test_sensor_has_no_caps(self):
        assert make_sensor()["caps"] == {}


class TestHelpers:
    def test_unsupported_and_read_only(self):
        assert not is_supported("vacuum")
        assert is_supported("light")
        assert is_read_only("sensor")
        assert not is_read_only("light")
        assert "vacuum" in UNSUPPORTED_DOMAINS

    def test_caps_unit(self):
        assert caps_unit({"brightness": {}}, "brightness") == "percent"
        assert caps_unit({"color_temp": {}}, "color_temp") == "kelvin"
        assert caps_unit({"temperature": {}}, "temperature") == "celsius"
        assert caps_unit({"hvac_modes": []}, "hvac_modes") == "enum"
        assert caps_unit({}, "unknown") == ""

    def test_boolean_is_not_treated_as_number(self):
        caps = build_caps("cover", {"current_position": True, "supported_features": 4})
        assert caps["position"] == {"min": 0, "max": 100}
