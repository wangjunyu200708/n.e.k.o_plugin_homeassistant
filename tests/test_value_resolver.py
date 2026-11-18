"""数值抽取、单位推断、相对量与钳值。"""

import pytest
from nlp.value_resolver import (
    extract_number,
    infer_prop_from_unit,
    parse_delta,
    parse_prop_value,
    resolve_adjust_target,
)


class TestExtractNumber:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("50%", (50, "%")),
            ("26度", (26, "度")),
            ("3000K", (3000, "K")),
            ("2档", (2, "档")),
            ("调到23.5度", (23.5, "度")),
            ("没有数字", None),
        ],
    )
    def test_arabic(self, text, expected):
        assert extract_number(text) == expected

    def test_chinese_number(self):
        assert extract_number("二十六度") == (26, "度")

    def test_chinese_two(self):
        assert extract_number("两档") == (2, "档")

    def test_quantifier_not_number(self):
        assert extract_number("一点") is None


class TestInferProp:
    def test_temperature(self):
        assert infer_prop_from_unit("度", 26) == "温度"
        assert infer_prop_from_unit("℃", 26) == "温度"

    def test_kelvin(self):
        assert infer_prop_from_unit("K", 3000) == "色温"

    def test_level(self):
        assert infer_prop_from_unit("档", 2) == "风速"

    def test_percent_defaults_to_brightness(self):
        assert infer_prop_from_unit("%", 50) == "亮度"

    def test_bare_int_in_range(self):
        assert infer_prop_from_unit("", 50) == "亮度"

    def test_bare_int_out_of_range(self):
        assert infer_prop_from_unit("", 300) is None


class TestParsePropValue:
    def test_with_prop(self):
        assert parse_prop_value("亮度50%") == ("亮度", 50, "%")

    def test_with_verb(self):
        assert parse_prop_value("调到26度") == (None, 26, "度")

    def test_chinese(self):
        assert parse_prop_value("温度二十六度") == ("温度", 26, "度")

    def test_no_number(self):
        assert parse_prop_value("亮一点") is None


class TestParseDelta:
    def test_arabic(self):
        assert parse_delta("调高5度") == 5.0

    def test_chinese(self):
        assert parse_delta("调低两度") == 2.0

    def test_none(self):
        assert parse_delta("调高一点") is None


class TestAdjustTarget:
    def test_explicit_delta(self):
        assert resolve_adjust_target(50, 1, 10, [0, 100, 1]) == 60

    def test_negative_direction(self):
        assert resolve_adjust_target(50, -1, 10, [0, 100, 1]) == 40

    def test_default_step_is_ten_percent(self):
        assert resolve_adjust_target(50, 1, None, [0, 100, 1]) == 60

    def test_clamped_to_max(self):
        assert resolve_adjust_target(98, 1, 10, [0, 100, 1]) == 100

    def test_clamped_to_min(self):
        assert resolve_adjust_target(2, -1, 10, [0, 100, 1]) == 0

    def test_step_alignment(self):
        # 温度 step=0.5 时 26.3 → 26.5
        target = resolve_adjust_target(26.0, 1, 0.3, [16, 30, 0.5])
        assert target == 26.5

    def test_step_zero_not_dividing(self):
        assert resolve_adjust_target(50, 1, 5, [0, 100, 0]) == 55

    def test_empty_range_defaults(self):
        assert resolve_adjust_target(50, 1, 10, []) == 60
