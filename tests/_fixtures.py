"""测试用实体工厂。

刻意走 `build_entity()` 而不是手写 dict：caps 由真实代码推导，测试才能发现
"caps 结构变了但测试还在按旧结构断言"这类漂移。
"""

from ha_api.entities import build_entity


def make_entity(
    entity_id: str,
    name: str,
    *,
    state: str = "on",
    area: str = "",
    device_name: str = "",
    alias: str = "",
    attributes: dict | None = None,
    registry: dict | None = None,
) -> dict:
    attrs = {"friendly_name": name}
    attrs.update(attributes or {})
    entity = build_entity(
        {"entity_id": entity_id, "state": state, "attributes": attrs},
        registry_entry=registry,
        area_name=area,
        device_name=device_name,
        plugin_alias=alias,
    )
    assert entity is not None
    return entity


def make_light(name: str = "客厅吸顶灯", *, area: str = "客厅", state: str = "on", **kwargs) -> dict:
    attributes = {
        "supported_color_modes": ["brightness", "color_temp", "hs"],
        "brightness": 204,  # 80%
        "color_mode": "color_temp",
        "min_color_temp_kelvin": 2000,
        "max_color_temp_kelvin": 6500,
        "color_temp_kelvin": 4000,
    }
    attributes.update(kwargs.pop("attributes", {}))
    return make_entity("light.living_room_ceiling", name, state=state, area=area, attributes=attributes, **kwargs)


def make_plain_light(name: str = "阳台灯", *, area: str = "阳台", state: str = "on", **kwargs) -> dict:
    """只有 onoff 的灯：不可调亮度。"""
    attributes = {"supported_color_modes": ["onoff"]}
    attributes.update(kwargs.pop("attributes", {}))
    return make_entity("light.balcony", name, state=state, area=area, attributes=attributes, **kwargs)


def make_switch(name: str = "书房插座", *, area: str = "书房", state: str = "off", **kwargs) -> dict:
    return make_entity("switch.study_plug", name, state=state, area=area, **kwargs)


def make_climate(name: str = "主卧空调", *, area: str = "主卧", state: str = "cool", **kwargs) -> dict:
    attributes = {
        "hvac_modes": ["off", "cool", "heat", "auto", "dry", "fan_only"],
        "min_temp": 16,
        "max_temp": 30,
        "target_temp_step": 0.5,
        "temperature": 26,
        "current_temperature": 28.5,
        "fan_modes": ["low", "medium", "high", "auto"],
        "fan_mode": "auto",
        "preset_modes": ["none", "eco", "boost"],
    }
    attributes.update(kwargs.pop("attributes", {}))
    return make_entity("climate.bedroom_ac", name, state=state, area=area, attributes=attributes, **kwargs)


def make_cover(
    name: str = "客厅窗帘", *, area: str = "客厅", state: str = "open", device_class: str = "curtain", **kwargs
) -> dict:
    attributes = {"current_position": 70, "device_class": device_class}
    attributes.update(kwargs.pop("attributes", {}))
    return make_entity("cover.living_room_curtain", name, state=state, area=area, attributes=attributes, **kwargs)


def make_garage(name: str = "车库门", *, area: str = "车库", state: str = "closed", **kwargs) -> dict:
    return make_cover(name, area=area, state=state, device_class="garage", **kwargs)


def make_media(name: str = "客厅音箱", *, area: str = "客厅", state: str = "playing", **kwargs) -> dict:
    attributes = {
        "volume_level": 0.35,
        "is_volume_muted": False,
        "source": "Spotify",
        "source_list": ["Spotify", "HDMI1", "HDMI2", "Bluetooth"],
        "media_title": "Bohemian Rhapsody",
        # PAUSE(1) | VOLUME_SET(4) | PREVIOUS(16) | NEXT(32) | SELECT_SOURCE(2048) | PLAY(16384)
        "supported_features": 1 + 4 + 16 + 32 + 2048 + 16384,
    }
    attributes.update(kwargs.pop("attributes", {}))
    return make_entity("media_player.living_room_speaker", name, state=state, area=area, attributes=attributes, **kwargs)


def make_lock(name: str = "大门门锁", *, area: str = "玄关", state: str = "locked", **kwargs) -> dict:
    return make_entity("lock.front_door", name, state=state, area=area, **kwargs)


def make_scene(name: str = "回家", *, entity_id: str = "", **kwargs) -> dict:
    return make_entity(
        entity_id or f"scene.{name}",
        name,
        state="scalar",
        area=kwargs.pop("area", ""),
        **kwargs,
    )


def make_sensor(name: str = "客厅温度", *, area: str = "客厅", state: str = "24.5", unit: str = "°C", **kwargs) -> dict:
    return make_entity(
        "sensor.living_room_temperature",
        name,
        state=state,
        area=area,
        attributes={"unit_of_measurement": unit, "device_class": "temperature"},
        **kwargs,
    )


def make_all() -> list[dict]:
    """一套覆盖六大 domain 的实体表，供路由/多指令测试使用。"""
    return [
        make_light(),
        make_plain_light(),
        make_switch(),
        make_climate(),
        make_cover(),
        make_media(),
        make_scene("回家", entity_id="scene.home_arrival"),
        make_scene("晚安", entity_id="scene.good_night"),
        make_sensor(),
    ]
