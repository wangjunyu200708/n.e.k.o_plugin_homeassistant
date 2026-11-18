"""能力校验与属性消歧。"""

from _fixtures import make_climate, make_cover, make_light, make_lock, make_media, make_plain_light, make_switch
from nlp.capability import (
    available_props,
    cap_for_prop,
    command_unit,
    current_value,
    match_option,
    prop_bounds,
    resolve_percent_prop,
    validate_command,
)


class TestCapForProp:
    def test_mode_disambiguation(self):
        assert cap_for_prop(make_climate(), "模式") == "hvac_modes"
        assert cap_for_prop(make_media(), "模式") == "source_list"
        assert cap_for_prop(make_light(), "模式") == "hvac_modes"  # 无 effect_list 时退回

    def test_mode_as_effect_when_light_has_effects(self):
        entity = make_light(attributes={"effect_list": ["Rainbow", "Cinema"]})
        assert cap_for_prop(entity, "模式") == "effect_list"

    def test_plain_props(self):
        assert cap_for_prop(make_light(), "亮度") == "brightness"
        assert cap_for_prop(make_cover(), "开合度") == "position"
        assert cap_for_prop(make_media(), "音量") == "volume"
        assert cap_for_prop(make_light(), "不存在") == ""


class TestAvailableProps:
    def test_light_lists_brightness_and_color(self):
        props = available_props(make_light())
        assert "亮度" in props
        assert "颜色" in props
        assert "开合度" not in props

    def test_synonyms_collapsed(self):
        props = available_props(make_light())
        assert "开度" not in props
        assert "色彩" not in props

    def test_onoff_light_has_minimal_props(self):
        assert available_props(make_plain_light()) == []


class TestResolvePercent:
    def test_light_keeps_brightness(self):
        assert resolve_percent_prop(make_light(), "亮度") == "亮度"

    def test_switch_without_brightness_falls_back(self):
        assert resolve_percent_prop(make_switch(), "亮度") == "亮度"

    def test_media_without_brightness_becomes_volume(self):
        assert resolve_percent_prop(make_media(), "亮度") == "音量"

    def test_cover_without_brightness_becomes_position(self):
        assert resolve_percent_prop(make_cover(), "亮度") == "开合度"

    def test_non_percent_prop_untouched(self):
        assert resolve_percent_prop(make_light(), "色温") == "色温"


class TestBounds:
    def test_light_brightness_bounds(self):
        assert prop_bounds(make_light(), "brightness") == [1, 100]

    def test_climate_temperature_bounds(self):
        assert prop_bounds(make_climate(), "temperature") == [16.0, 30.0, 0.5]

    def test_unknown_cap_returns_empty(self):
        assert prop_bounds(make_light(), "nope") == []


class TestCurrentValue:
    def test_brightness_converted(self):
        assert current_value(make_light(), "brightness") == 80

    def test_volume_converted(self):
        assert current_value(make_media(), "volume") == 35

    def test_position(self):
        assert current_value(make_cover(), "position") == 70

    def test_temperature(self):
        assert current_value(make_climate(), "temperature") == 26

    def test_color_temp_kelvin(self):
        assert current_value(make_light(), "color_temp") == 4000

    def test_color_temp_from_mireds(self):
        entity = make_light(attributes={"color_temp_kelvin": None, "color_temp": 250})
        assert current_value(entity, "color_temp") == 4000

    def test_missing(self):
        assert current_value(make_switch(), "brightness") is None


class TestMatchOption:
    def test_exact(self):
        assert match_option(["low", "high"], "high") == "high"

    def test_case_insensitive(self):
        assert match_option(["High"], "high") == "High"

    def test_synonym_chinese_to_english(self):
        assert match_option(["low", "medium", "high"], "高") == "high"
        assert match_option(["low", "medium", "high"], "中") == "medium"

    def test_hvac_synonyms(self):
        assert match_option(["off", "cool", "heat"], "制冷") == "cool"
        assert match_option(["off", "cool", "heat"], "制热") == "heat"

    def test_substring(self):
        assert match_option(["auto_fan"], "auto") == "auto_fan"

    def test_no_match(self):
        assert match_option(["low", "high"], "彩虹") is None

    def test_empty(self):
        assert match_option([], "high") is None
        assert match_option(["high"], "") is None


class TestValidate:
    def test_valid_brightness(self):
        result = validate_command(make_light(), "亮度", 50, "%")
        assert result.status == "valid"
        assert result.resolved == 50

    def test_invalid_unit(self):
        result = validate_command(make_light(), "亮度", 3, "档")
        assert result.status == "invalid_unit"
        assert result.detail["expected_unit"] == "percent"
        assert "%" in result.detail["allowed_units"]

    def test_invalid_value_out_of_range(self):
        result = validate_command(make_light(), "亮度", 150, "%")
        assert result.status == "invalid_value"
        assert result.detail["max"] == 100

    def test_climate_temperature_range(self):
        result = validate_command(make_climate(), "温度", 40, "度")
        assert result.status == "invalid_value"
        assert result.detail["min"] == 16.0

    def test_climate_temperature_valid(self):
        assert validate_command(make_climate(), "温度", 26, "度").status == "valid"

    def test_enum_resolved(self):
        result = validate_command(make_climate(), "模式", "制冷", "")
        assert result.status == "valid"
        assert result.resolved == "cool"

    def test_enum_invalid(self):
        result = validate_command(make_climate(), "模式", "彩虹", "")
        assert result.status == "enum_value_invalid"
        assert "cool" in result.detail["allowed"]

    def test_unknown_attribute(self):
        result = validate_command(make_switch(), "亮度", 50, "%")
        assert result.status == "unknown_attribute"
        assert result.detail["available"] == []

    def test_color_always_valid(self):
        assert validate_command(make_light(), "颜色", 0xFF0000, "").status == "valid"

    def test_volume_percent(self):
        assert validate_command(make_media(), "音量", 30, "%").status == "valid"

    def test_lock_has_no_props(self):
        assert validate_command(make_lock(), "亮度", 50, "%").status == "unknown_attribute"


class TestCommandUnit:
    def test_units(self):
        assert command_unit("度") == "celsius"
        assert command_unit("%") == "percent"
        assert command_unit("档") == "level"
        assert command_unit("K") == "kelvin"
        assert command_unit("未知") is None
