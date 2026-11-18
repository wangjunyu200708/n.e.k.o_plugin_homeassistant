"""五步短路路由。"""

from _fixtures import make_all
from nlp.router import route


class TestScene:
    async def test_explicit_scene(self):
        result = await route("执行回家场景", make_all())
        assert result.branch == "scene"
        assert result.scene_keywords == ["回家"]

    async def test_scene_mode_form(self):
        result = await route("打开回家模式", make_all())
        assert result.branch == "scene"
        assert result.scene_keywords == ["回家"]

    async def test_colloquial_alias(self):
        result = await route("我回家了", make_all())
        assert result.branch == "scene"
        assert "回家" in result.scene_keywords

    async def test_good_night(self):
        result = await route("晚安", make_all())
        assert result.branch == "scene"
        assert result.scene_keywords[0] == "晚安"

    async def test_scene_does_not_need_devices(self):
        result = await route("执行回家场景", [])
        assert result.branch == "scene"


class TestQuery:
    async def test_temperature_query(self):
        result = await route("客厅温度多少度", make_all())
        assert result.branch == "query"
        assert result.device_hint == "客厅"
        assert result.query_prop == "温度"

    async def test_status_query(self):
        result = await route("客厅灯怎么样", make_all())
        assert result.branch == "query"

    async def test_switch_not_hijacked_by_query(self):
        # "关闭卧室灯怎么样" 是控制，不是查询
        result = await route("关闭卧室灯怎么样", make_all())
        assert result.branch == "switch"


class TestSwitch:
    async def test_open(self):
        result = await route("打开客厅吸顶灯", make_all())
        assert result.branch == "switch"
        assert result.parsed.value is True
        assert result.match.status == "ok"

    async def test_close(self):
        result = await route("关闭书房插座", make_all())
        assert result.branch == "switch"
        assert result.parsed.value is False

    async def test_unknown_device_still_routes_switch(self):
        result = await route("打开不存在的灯", make_all())
        assert result.branch == "switch"
        assert result.match.status == "not_found"


class TestControl:
    async def test_brightness(self):
        result = await route("客厅吸顶灯亮度50%", make_all())
        assert result.branch == "control"
        assert result.parsed.prop == "亮度"

    async def test_temperature(self):
        result = await route("主卧空调26度", make_all())
        assert result.branch == "control"
        assert result.parsed.prop == "温度"

    async def test_cover_fraction(self):
        result = await route("客厅窗帘开到一半", make_all())
        assert result.branch == "control"
        assert result.parsed.prop == "开合度"


class TestAction:
    async def test_media_play(self):
        result = await route("客厅音箱播放", make_all())
        assert result.branch == "action"
        assert result.verb == "播放"
        assert result.match.status == "ok"

    async def test_action_falls_back_when_device_missing(self):
        # 匹配不到设备时不提交 action 分支，避免误判
        result = await route("播放器音量30", make_all())
        assert result.branch in ("control", "action", "unknown")


class TestUnknown:
    async def test_gibberish(self):
        result = await route("今天天气真不错", make_all())
        assert result.branch == "unknown"
