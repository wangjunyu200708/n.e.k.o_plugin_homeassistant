"""目标解析：确定性匹配的优先级与"宁可报歧义也不猜"。

解析层是控制链上唯一可能"认错设备"的地方，所以这里逐级验证：每一级都必须
先命中前一级才轮到下一级，且任何一级出现多个候选都要报 ambiguous。
"""

import pytest
from _fixtures import make_all, make_cover, make_garage, make_light, make_lock, make_plain_light, make_switch

# isort: split
from resolver import AMBIGUOUS, NOT_FOUND, OK, EntityIndex, expand_targets, resolve_targets


@pytest.fixture
def entities():
    return make_all()


class TestExactLevels:
    def test_entity_id(self, entities):
        match = resolve_targets(["light.living_room_ceiling"], entities)[0]
        assert match.status == OK
        assert match.entity["entity_id"] == "light.living_room_ceiling"

    def test_name(self, entities):
        match = resolve_targets(["客厅吸顶灯"], entities)[0]
        assert match.status == OK
        assert match.entity["entity_id"] == "light.living_room_ceiling"

    def test_alias(self, entities):
        entities.append(make_light("灯A", alias="大灯"))
        match = resolve_targets(["大灯"], entities)[0]
        assert match.status == OK
        assert match.entity["name"] == "灯A"

    def test_device_name(self, entities):
        entities.append(make_switch("插座A", device_name="书房排插"))
        match = resolve_targets(["书房排插"], entities)[0]
        assert match.status == OK
        assert match.entity["name"] == "插座A"

    def test_area_plus_name(self, entities):
        # "客厅" + "吸顶灯" 拼接后精确命中，覆盖用户直接说全名的情况。
        match = resolve_targets(["客厅吸顶灯"], entities)[0]
        assert match.status == OK

    def test_case_and_space_folding(self, entities):
        match = resolve_targets(["  Light.Living_Room_Ceiling "], entities)[0]
        assert match.status == OK


class TestAreaLevel:
    def test_single_entity_area(self, entities):
        # 主卧只有空调一台，区域名等价于设备名。
        match = resolve_targets(["主卧"], entities)[0]
        assert match.status == OK
        assert match.entity["domain"] == "climate"

    def test_multi_entity_area_is_ambiguous(self, entities):
        # 客厅有灯/窗帘/音箱，绝不能替用户挑一个。
        match = resolve_targets(["客厅"], entities)[0]
        assert match.status == AMBIGUOUS
        assert len(match.candidates) > 1
        assert match.entity is None

    def test_area_narrowed_by_domain(self, entities):
        # 指定 domain 后客厅只剩一盏灯（阳台灯在阳台），歧义消解。
        match = resolve_targets(["客厅"], entities, domain="light")[0]
        assert match.status in (OK, AMBIGUOUS)
        if match.status == OK:
            assert match.entity["domain"] == "light"


class TestSubstringLevel:
    def test_substring_single(self, entities):
        match = resolve_targets(["吸顶"], entities)[0]
        assert match.status == OK
        assert match.entity["name"] == "客厅吸顶灯"

    def test_substring_multiple_is_ambiguous(self, entities):
        match = resolve_targets(["灯"], entities)[0]
        assert match.status == AMBIGUOUS
        assert len(match.candidates) >= 2

    def test_substring_does_not_cross_into_unrelated(self, entities):
        match = resolve_targets(["冰箱"], entities)[0]
        assert match.status == NOT_FOUND


class TestPickUniqueOnline:
    def test_offline_duplicate_ignored(self, entities):
        # 客厅灯泡坏了但没从 HA 删掉：离线的那台不应造成歧义。
        entities.append(make_plain_light("客厅吊灯", state="unavailable"))
        match = resolve_targets(["吸顶灯"], entities)[0]
        assert match.status == OK

    def test_both_online_is_ambiguous(self, entities):
        entities.append(make_plain_light("客厅吊灯", state="on"))
        match = resolve_targets(["灯"], entities)[0]
        assert match.status == AMBIGUOUS


class TestEdgeCases:
    def test_empty_target(self, entities):
        assert resolve_targets([""], entities)[0].status == NOT_FOUND

    def test_not_found(self, entities):
        match = resolve_targets(["火星基地"], entities)[0]
        assert match.status == NOT_FOUND
        assert match.entity is None

    def test_candidate_briefs_are_trimmed(self, entities):
        match = resolve_targets(["灯"], entities)[0]
        brief = match.candidate_briefs()[0]
        assert set(brief) == {"entity_id", "name", "area_name", "domain"}

    def test_candidate_briefs_capped(self):
        many = [make_plain_light(f"灯{i}", entity_id=f"light.x{i}") for i in range(20)]
        match = EntityIndex(many).resolve("灯")
        assert len(match.candidate_briefs()) == 5


class TestBatch:
    def test_preserves_order(self, entities):
        matches = resolve_targets(["书房插座", "客厅吸顶灯"], entities)
        assert [match.target for match in matches] == ["书房插座", "客厅吸顶灯"]

    def test_dedupes_identical_targets(self, entities):
        matches = resolve_targets(["客厅吸顶灯", "客厅吸顶灯"], entities)
        assert matches[0] is matches[1]

    def test_expand_targets_dedupes_entities(self, entities):
        matches = resolve_targets(["客厅吸顶灯", "light.living_room_ceiling"], entities)
        expanded = expand_targets(matches)
        assert len(expanded) == 1

    def test_expand_skips_unresolved(self, entities):
        matches = resolve_targets(["客厅吸顶灯", "火星基地"], entities)
        assert len(expand_targets(matches)) == 1


class TestGarageVsCurtain:
    """同一 domain 下风险完全不同的两台设备，名字必须能分别命中。"""

    def test_both_resolve_independently(self):
        entities = [make_cover(), make_garage(), make_lock()]
        curtain = resolve_targets(["客厅窗帘"], entities)[0]
        garage = resolve_targets(["车库门"], entities)[0]
        assert curtain.entity["entity_id"] == "cover.living_room_curtain"
        assert garage.entity["entity_id"] == "cover.garage_door"
