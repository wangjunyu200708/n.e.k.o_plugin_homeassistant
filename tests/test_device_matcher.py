"""实体匹配：别名 > 名字 > 区域+名字 > 模糊。"""

from _fixtures import make_all, make_entity, make_light, make_scene
from nlp.device_matcher import find_scene, match_devices, summarize_candidates


class TestExact:
    def test_exact_name(self):
        result = match_devices("客厅吸顶灯", make_all())
        assert result.status == "ok"
        assert result.device["entity_id"] == "light.living_room_ceiling"

    def test_alias_beats_name(self):
        devices = [
            make_entity("light.a", "Light A", alias="大灯"),
            make_entity("light.b", "大灯2"),
        ]
        result = match_devices("大灯", devices)
        assert result.status == "ok"
        assert result.device["entity_id"] == "light.a"

    def test_normalizer_strips_de(self):
        result = match_devices("客厅的吸顶灯", make_all())
        assert result.status == "ok"

    def test_multiple_exact_is_ambiguous(self):
        devices = [make_entity("light.a", "顶灯"), make_entity("light.b", "顶灯")]
        result = match_devices("顶灯", devices)
        assert result.status == "ambiguous"
        assert len(result.candidates) == 2
        assert "2 个" in result.message


class TestAreaSplit:
    def test_area_plus_name(self):
        devices = [
            make_light("吸顶灯", area="客厅"),
            make_light("吸顶灯", area="卧室"),
        ]
        # 两个同名实体，靠区域限定区分
        devices[1]["entity_id"] = "light.bedroom_ceiling"
        result = match_devices("卧室吸顶灯", devices)
        assert result.status == "ok"
        assert result.device["area_name"] == "卧室"

    def test_area_only_match_returns_ambiguous(self):
        devices = [
            make_entity("light.a", "射灯1", area="客厅"),
            make_entity("light.b", "射灯2", area="客厅"),
        ]
        result = match_devices("客厅射灯", devices)
        assert result.status == "ambiguous"

    def test_no_area_data_falls_back_to_fuzzy(self):
        # 非管理员令牌拿不到区域名 → area_name 全空
        devices = [make_entity("light.a", "客厅灯"), make_entity("switch.b", "插座")]
        result = match_devices("客厅灯", devices)
        assert result.status == "ok"
        assert result.device["entity_id"] == "light.a"


class TestFuzzy:
    def test_substring_both_directions(self):
        result = match_devices("吸顶", make_all())
        assert result.status == "ok"

    def test_alias_substring(self):
        devices = [make_entity("light.a", "Philips Hue", alias="床头台灯")]
        result = match_devices("床头", devices)
        assert result.status == "ok"

    def test_not_found_lists_devices(self):
        result = match_devices("不存在的设备", make_all())
        assert result.status == "not_found"
        assert "未找到" in result.message
        assert result.candidates

    def test_empty_devices(self):
        result = match_devices("灯", [])
        assert result.status == "not_found"
        assert "未配置" in result.message or "为空" in result.message


class TestScenes:
    def test_find_scene_by_keyword(self):
        scene = find_scene(make_all(), ["回家"])
        assert scene is not None
        assert scene["name"] == "回家"

    def test_find_scene_second_keyword(self):
        # "晚安" 找不到时用 "睡觉" 兜底
        scene = find_scene(make_all(), ["睡觉", "晚安"])
        assert scene is not None
        assert scene["name"] == "晚安"

    def test_find_scene_missing(self):
        assert find_scene(make_all(), ["不存在的场景"]) is None

    def test_scene_excluded_from_control_pool_but_found(self):
        devices = [make_scene("回家")]
        assert find_scene(devices, ["回家"])["entity_id"] == "scene.回家"


class TestSummarize:
    def test_summary_fields(self):
        summary = summarize_candidates([make_light()])[0]
        assert set(summary) == {"name", "area", "domain", "entity_id", "alias"}
        assert summary["area"] == "客厅"
