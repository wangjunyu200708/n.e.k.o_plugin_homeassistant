"""控制命令解析。"""

import pytest
from nlp.control_parser import parse_control_command


@pytest.mark.parametrize(
    "command,device,action,prop,value",
    [
        ("打开客厅灯", "客厅灯", "switch", None, True),
        ("关闭卧室插座", "卧室插座", "switch", None, False),
        ("开灯", "灯", "switch", None, True),
        ("客厅灯关掉", "客厅灯", "switch", None, False),
        ("空调打开", "空调", "switch", None, True),
        ("把客厅灯亮度调到50%", "客厅灯", "set_prop", "亮度", 50),
        ("灯亮度30", "灯", "set_prop", "亮度", 30),
        ("空调调到26度", "空调", "set_prop", "温度", 26),
        ("空调26度", "空调", "set_prop", "温度", 26),
        ("灯色温3000K", "灯", "set_prop", "色温", 3000),
    ],
)
def test_basic_parsing(command, device, action, prop, value):
    result = parse_control_command(command)
    assert result is not None, command
    assert result.device == device
    assert result.action == action
    if prop is not None:
        assert result.prop == prop
    if value is not None:
        assert result.value == value


class TestModes:
    def test_hvac_mode_translated(self):
        result = parse_control_command("空调制冷")
        assert result.action == "set_prop"
        assert result.prop == "模式"
        assert result.value == "cool"

    def test_hvac_mode_with_verb(self):
        result = parse_control_command("空调调到制热")
        assert result.prop == "模式"
        assert result.value == "heat"

    def test_unknown_mode_word_passed_through(self):
        # 灯效/音源无法穷举，原样透出交给 match_option
        result = parse_control_command("客厅灯调到影院")
        assert result.prop == "模式"
        assert result.value == "影院"


class TestAdjust:
    def test_brighter(self):
        result = parse_control_command("客厅灯调亮一点")
        assert result.action == "adjust_prop"
        assert result.prop == "亮度"
        assert result.direction == 1

    def test_dimmer_with_delta(self):
        result = parse_control_command("灯调暗两档")
        assert result.direction == -1

    def test_temperature_delta(self):
        result = parse_control_command("空调温度调高5度")
        assert result.prop == "温度"
        assert result.direction == 1
        assert result.delta == 5.0


class TestExtremes:
    def test_max(self):
        result = parse_control_command("灯亮度调到最高")
        assert result.value == "max"

    def test_min(self):
        # 必须带设备名：只有属性词会被判为"没有目标设备"而放弃解析
        result = parse_control_command("音箱音量调到最低")
        assert result is not None
        assert result.device == "音箱"
        assert result.prop == "音量"
        assert result.value == "min"


class TestColors:
    def test_red(self):
        result = parse_control_command("客厅灯调到红色")
        assert result.prop == "颜色"
        assert result.value == 0xFF0000

    def test_unknown_color_not_matched_as_color(self):
        result = parse_control_command("客厅灯调到彩虹")
        # 不是已知颜色词，落到枚举分支或 None，但绝不当成颜色
        assert result is None or result.prop != "颜色"


class TestFractions:
    @pytest.mark.parametrize(
        "command,value",
        [
            ("窗帘开到一半", 50),
            ("窗帘开到大半", 70),
            ("窗帘开到三分之一", 33),
        ],
    )
    def test_fraction(self, command, value):
        result = parse_control_command(command)
        assert result is not None, command
        assert result.prop == "开合度"
        assert result.value == value

    def test_volume_fraction(self):
        result = parse_control_command("音箱音量调到四分之一")
        assert result.prop == "音量"
        assert result.value == 25


class TestKelvin:
    def test_kelvin_unit(self):
        result = parse_control_command("客厅灯色温3000K")
        assert result is not None
        assert result.prop == "色温"
        assert result.value == 3000
        assert result.unit == "K"


class TestEnumValue:
    def test_english_enum(self):
        result = parse_control_command("音箱音源HDMI1")
        assert result is not None
        assert result.prop == "音源"
        assert result.value == "HDMI1"


class TestGuards:
    def test_scene_command_not_parsed_here(self):
        assert parse_control_command("执行回家场景") is None

    def test_action_verb_prefix_not_switch(self):
        result = parse_control_command("开始播放")
        # 不能被"开"切出设备"始播放"
        assert result is None or result.device != "始播放"

    def test_pure_question_returns_none(self):
        assert parse_control_command("今天天气怎么样") is None
