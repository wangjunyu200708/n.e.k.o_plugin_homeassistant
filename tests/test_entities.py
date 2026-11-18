"""实体合并：state + 注册表 → 统一视图。"""

from _fixtures import make_entity, make_light

# isort: split
from ha_api.entities import (
    build_entity,
    filter_controllable,
    index_registries,
    resolve_area,
    state_facts,
)


class TestBuildEntity:
    def test_friendly_name_wins(self):
        entity = make_entity("light.a", "客厅吸顶灯", attributes={"friendly_name": "覆盖后的名字"})
        assert entity["name"] == "覆盖后的名字"

    def test_registry_name_used_when_no_friendly_name(self):
        entity = build_entity(
            {"entity_id": "light.a", "state": "on", "attributes": {}},
            registry_entry={"entity_id": "light.a", "name": "注册表名"},
        )
        assert entity["name"] == "注册表名"

    def test_original_name_then_entity_id_fallback(self):
        entity = build_entity(
            {"entity_id": "light.a", "state": "on", "attributes": {}},
            registry_entry={"entity_id": "light.a", "original_name": "原始名"},
        )
        assert entity["name"] == "原始名"

        bare = build_entity({"entity_id": "light.b", "state": "on", "attributes": {}})
        assert bare["name"] == "light.b"

    def test_invalid_entity_id_returns_none(self):
        assert build_entity({"entity_id": "noperiod", "state": "on"}) is None
        assert build_entity({"state": "on"}) is None

    def test_attributes_are_slimmed(self):
        entity = build_entity(
            {
                "entity_id": "light.a",
                "state": "on",
                "attributes": {"friendly_name": "灯", "brightness": 10, "entity_picture": "/api/camera_proxy/xxx?token=secret"},
            }
        )
        assert "entity_picture" not in entity["attributes"]
        assert entity["attributes"]["brightness"] == 10

    def test_registry_and_plugin_alias_merge(self):
        entity = make_entity("light.a", "灯", registry={"aliases": ["顶灯", "主灯"]}, alias="大灯")
        assert entity["alias"] == "顶灯,主灯,大灯"

    def test_offline_state(self):
        assert make_entity("light.a", "灯", state="unavailable")["is_online"] is False
        assert make_entity("light.b", "灯", state="on")["is_online"] is True


class TestRegistry:
    def test_resolve_area_prefers_entity_area(self):
        devices = {"dev1": {"id": "dev1", "area_id": "area_bed", "name": "空调设备"}}
        areas = {"area_living": "客厅", "area_bed": "主卧"}

        area, device_name = resolve_area({"entity_id": "light.a", "device_id": "dev1", "area_id": "area_living"}, devices, areas)
        assert area == "客厅"
        assert device_name == "空调设备"

    def test_resolve_area_inherits_from_device(self):
        devices = {"dev1": {"id": "dev1", "area_id": "area_bed", "name_by_user": "主卧空调"}}
        area, device_name = resolve_area({"entity_id": "climate.a", "device_id": "dev1"}, devices, {"area_bed": "主卧"})
        assert area == "主卧"
        assert device_name == "主卧空调"

    def test_resolve_area_missing_everything(self):
        area, device_name = resolve_area({}, {}, {})
        assert area == ""
        assert device_name == ""

    def test_index_registries(self):
        entities, devices, areas = index_registries(
            [{"entity_id": "light.a", "device_id": "d1"}],
            [{"id": "d1", "area_id": "a1"}],
            [{"area_id": "a1", "name": "客厅"}],
        )
        assert entities["light.a"]["device_id"] == "d1"
        assert devices["d1"]["area_id"] == "a1"
        assert areas["a1"] == "客厅"


class TestStateFacts:
    def test_light_brightness_converted_to_percent(self):
        facts = state_facts(make_light())
        assert facts["power"] is True
        assert facts["brightness"] == 80

    def test_climate_facts(self):
        from _fixtures import make_climate

        facts = state_facts(make_climate())
        assert facts["hvac_mode"] == "cool"
        assert facts["current_temperature"] == 28.5
        assert facts["target_temperature"] == 26

    def test_media_volume_converted_to_percent(self):
        from _fixtures import make_media

        facts = state_facts(make_media())
        assert facts["volume"] == 35
        assert facts["source"] == "Spotify"

    def test_lock_facts(self):
        from _fixtures import make_lock

        assert state_facts(make_lock())["locked"] is True

    def test_cover_facts(self):
        from _fixtures import make_cover

        facts = state_facts(make_cover())
        assert facts["position"] == 70


class TestFilter:
    def test_hidden_and_disabled_excluded(self):
        healthy = make_entity("light.a", "灯")
        hidden = make_entity("light.b", "灯2", registry={"hidden_by": "user"})
        disabled = make_entity("light.c", "灯3", registry={"disabled_by": "user"})
        out = filter_controllable([healthy, hidden, disabled])
        assert [item["entity_id"] for item in out] == ["light.a"]

    def test_read_only_excluded_by_default(self):
        from _fixtures import make_sensor

        out = filter_controllable([make_light(), make_sensor()])
        assert len(out) == 1
        out_all = filter_controllable([make_light(), make_sensor()], include_read_only=True)
        assert len(out_all) == 2
